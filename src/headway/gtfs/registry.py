"""Registry of static GTFS snapshots and resolution of the version valid for a date.

The registry is ``<gtfs_dir>/gtfs_versions.parquet``: one row per stored snapshot with its
snapshot date (UTC), SHA-256, size, path relative to ``gtfs_dir`` and the date range the
feed covers. The range comes from ``feed_info.txt`` (``feed_start_date``/``feed_end_date``)
or, if that is missing, from ``calendar.txt`` and ``calendar_dates.txt``.

The only way to obtain a :class:`ResolvedGtfs`, the object every GTFS consumer needs, is
:func:`resolve_gtfs`, so schedule data is never used without choosing its version first.
"""

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from headway.fileio import write_atomic

REGISTRY_NAME = "gtfs_versions.parquet"
SNAPSHOT_NAME = re.compile(r"^PID_GTFS_(\d{4}-\d{2}-\d{2})_([0-9a-f]{12})\.zip$")
REGISTRY_SCHEMA = pa.schema(
    [
        pa.field("snapshot_date", pa.date32(), nullable=False),
        pa.field("sha256", pa.string(), nullable=False),
        pa.field("bytes", pa.int64(), nullable=False),
        pa.field("path", pa.string(), nullable=False),
        pa.field("feed_start_date", pa.date32(), nullable=False),
        pa.field("feed_end_date", pa.date32(), nullable=False),
    ]
)
_HASH_CHUNK = 1 << 20


class GtfsVersionNotFoundError(LookupError):
    """No stored snapshot covers the requested service date."""


@dataclass(frozen=True, order=True)
class GtfsVersion:
    """One stored GTFS snapshot."""

    snapshot_date: date
    sha256: str
    bytes: int
    path: str
    feed_start_date: date
    feed_end_date: date

    def covers(self, service_date: date) -> bool:
        """True if the feed's date range includes ``service_date``."""
        return self.feed_start_date <= service_date <= self.feed_end_date


_RESOLVE_TOKEN = object()


@dataclass(frozen=True)
class ResolvedGtfs:
    """The GTFS version chosen for one service date. Create it with :func:`resolve_gtfs`."""

    service_date: date
    version: GtfsVersion
    gtfs_dir: Path
    _token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Refuse construction outside :func:`resolve_gtfs`."""
        if self._token is not _RESOLVE_TOKEN:
            raise TypeError("ResolvedGtfs can only be created by resolve_gtfs()")

    @property
    def zip_path(self) -> Path:
        """Absolute path of the snapshot zip."""
        return self.gtfs_dir / self.version.path


def sha256_file(path: Path) -> str:
    """Return the SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _yyyymmdd(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y%m%d").date()


def _rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    if name not in archive.namelist():
        return []
    text = archive.read(name).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def feed_date_range(zip_path: Path) -> tuple[date, date]:
    """Return the first and last service date covered by a GTFS zip."""
    with zipfile.ZipFile(zip_path) as archive:
        info = _rows(archive, "feed_info.txt")
        if info and info[0].get("feed_start_date") and info[0].get("feed_end_date"):
            return _yyyymmdd(info[0]["feed_start_date"]), _yyyymmdd(info[0]["feed_end_date"])
        dates = [
            _yyyymmdd(row[key])
            for row in _rows(archive, "calendar.txt")
            for key in ("start_date", "end_date")
        ]
        dates += [
            _yyyymmdd(row["date"])
            for row in _rows(archive, "calendar_dates.txt")
            if row.get("exception_type", "").strip() == "1"
        ]
    if not dates:
        raise ValueError(f"{zip_path.name}: no feed_info dates and no calendar dates")
    return min(dates), max(dates)


def describe_snapshot(zip_path: Path, gtfs_dir: Path) -> GtfsVersion:
    """Build the registry entry for a stored snapshot, verifying its name against its hash."""
    match = SNAPSHOT_NAME.match(zip_path.name)
    if match is None:
        raise ValueError(f"not a snapshot file name: {zip_path.name}")
    digest = sha256_file(zip_path)
    if digest[:12] != match.group(2):
        raise ValueError(f"{zip_path.name}: content hash {digest[:12]} does not match name")
    start, end = feed_date_range(zip_path)
    return GtfsVersion(
        snapshot_date=date.fromisoformat(match.group(1)),
        sha256=digest,
        bytes=zip_path.stat().st_size,
        path=zip_path.relative_to(gtfs_dir).as_posix(),
        feed_start_date=start,
        feed_end_date=end,
    )


def read_registry(gtfs_dir: Path) -> list[GtfsVersion]:
    """Return all registered versions, oldest snapshot first (empty if no registry)."""
    path = gtfs_dir / REGISTRY_NAME
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = pq.read_table(path, schema=REGISTRY_SCHEMA).to_pylist()
    return sorted(GtfsVersion(**row) for row in rows)


def write_registry(gtfs_dir: Path, versions: list[GtfsVersion]) -> None:
    """Write the registry atomically, sorted and without duplicate hashes."""
    unique = {v.sha256: v for v in sorted(versions)}
    table = pa.Table.from_pylist(
        [asdict(v) for v in sorted(unique.values())], schema=REGISTRY_SCHEMA
    )
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    write_atomic(gtfs_dir / REGISTRY_NAME, sink.getvalue().to_pybytes())


def register_snapshot(gtfs_dir: Path, zip_path: Path) -> GtfsVersion:
    """Add one stored snapshot to the registry and return its entry."""
    version = describe_snapshot(zip_path, gtfs_dir)
    write_registry(gtfs_dir, [*read_registry(gtfs_dir), version])
    return version


def rebuild_registry(gtfs_dir: Path) -> list[GtfsVersion]:
    """Recreate the registry from every snapshot zip in ``gtfs_dir`` (slow: hashes all)."""
    versions = [
        describe_snapshot(p, gtfs_dir)
        for p in sorted(gtfs_dir.glob("PID_GTFS_*.zip"))
        if SNAPSHOT_NAME.match(p.name)
    ]
    write_registry(gtfs_dir, versions)
    return read_registry(gtfs_dir)


def resolve_gtfs(service_date: date, gtfs_dir: Path) -> ResolvedGtfs:
    """Return the GTFS version valid for ``service_date``.

    That is the newest snapshot taken on or before ``service_date`` whose feed range covers
    it: the timetable as published for that day, ignoring later corrections. Raises
    :class:`GtfsVersionNotFoundError` if no such snapshot exists.
    """
    candidates = [
        v
        for v in read_registry(gtfs_dir)
        if v.snapshot_date <= service_date and v.covers(service_date)
    ]
    if not candidates:
        raise GtfsVersionNotFoundError(f"no GTFS snapshot covers service date {service_date}")
    return ResolvedGtfs(service_date, max(candidates), gtfs_dir, _RESOLVE_TOKEN)
