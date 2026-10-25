"""Ingest of raw polls into the positions dataset, one service date at a time.

Each service date is rebuilt from scratch. The manifest is read for the polls that can
contain its trips, every observation is assigned to the service date of its trip (the
scheduled start, ``start_timestamp``), and that date's partition is replaced. Running
ingest again therefore gives identical files.

Accounting rules, so that nothing is lost silently:

- An observation (trip, scheduled start, origin time) is written once, to the partition of
  its trip's service date. Repeats across polls are counted, not written again; the row
  keeps the provenance of the first poll that reported it.
- A rejected poll or feature is reported by the service date that owns the poll, the one
  whose service day contains its fetch time, so each reject appears exactly once.
- A date reads the polls fetched from ``window_before_h`` hours before its service day
  until ``window_after_h`` hours after it. Positions outside that window are not read.
"""

import gzip
import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from headway.collect.manifest import PollRecord, manifest_path, read_manifest
from headway.config import IngestConfig, Settings
from headway.fileio import write_atomic
from headway.gtfs.registry import GtfsVersion, GtfsVersionNotFoundError, resolve_gtfs
from headway.ingest.parser import parse_poll
from headway.ingest.resources import peak_memory_mb
from headway.ingest.schema import POSITIONS_SCHEMA, REJECTS_SCHEMA

SYNTHETIC_MARKER = "SYNTHETIC.json"
DEFAULT_LIMIT = 100  # upstream default of the ``limit`` parameter, if the config lacks it
PART = "part-0.parquet"

# (trip, scheduled start, origin time): one position report of one trip.
ObservationKey = tuple[str, datetime, datetime]
# (vehicle, origin time, lat, lon): one vehicle message, which a handover repeats on 2 trips.
VehicleMessage = tuple[str, datetime, float | None, float | None]


@dataclass(frozen=True)
class IngestInputs:
    """Locations, settings and provenance of one archive."""

    raw_dir: Path
    manifest_dir: Path
    gtfs_dir: Path
    positions_dir: Path
    rejects_dir: Path
    reports_dir: Path
    cfg: IngestConfig
    limit: int
    tz: str
    synthetic: bool


def inputs_for(root: Path, settings: Settings) -> IngestInputs:
    """Return the ingest inputs of the archive rooted at ``root``.

    Derived data goes under ``<root>/<paths.data_dir>``, so a synthetic archive never mixes
    with real data.
    """
    paths = settings.paths
    data = root / paths.data_dir
    return IngestInputs(
        raw_dir=root / paths.raw_dir,
        manifest_dir=root / paths.manifest_dir,
        gtfs_dir=root / paths.gtfs_dir,
        positions_dir=data / "positions",
        rejects_dir=data / "rejects",
        reports_dir=data / "ingest_reports",
        cfg=settings.ingest,
        limit=int(settings.collect.params.get("limit", DEFAULT_LIMIT)),
        tz=settings.time.local_tz,
        synthetic=(root / SYNTHETIC_MARKER).exists(),
    )


def service_date_of(ts: datetime, tz: str, start_min: int) -> date:
    """Return the service date of an instant: the local date shifted back by the day start."""
    return (ts.astimezone(ZoneInfo(tz)) - timedelta(minutes=start_min)).date()


def day_start_utc(day: date, tz: str, start_min: int) -> datetime:
    """Return the UTC instant at which the service day of ``day`` begins."""
    local_midnight = datetime.combine(day, clock(0), tzinfo=ZoneInfo(tz))
    return (local_midnight + timedelta(minutes=start_min)).astimezone(UTC)


def read_manifests(manifest_dir: Path, start: datetime, end: datetime) -> list[PollRecord]:
    """Return the manifest records fetched in ``[start, end)``, ordered by fetch time."""
    records: list[PollRecord] = []
    day = start.date()
    while day <= end.date():
        path = manifest_path(manifest_dir, day)
        if path.exists():
            records += [r for r in read_manifest(path) if start <= r.fetched_at < end]
        day += timedelta(days=1)
    return sorted(records, key=lambda r: (r.fetched_at, r.attempt))


def available_service_dates(inputs: IngestInputs) -> list[date]:
    """Return the service dates that own at least one manifest record, oldest first."""
    start_min = inputs.cfg.service_day_start_min
    dates: set[date] = set()
    for path in sorted(inputs.manifest_dir.glob("*.parquet")):
        for record in read_manifest(path):
            dates.add(service_date_of(record.fetched_at, inputs.tz, start_min))
    return sorted(dates)


def _read_body(raw_dir: Path, record: PollRecord) -> tuple[bytes | None, str, str]:
    """Return the body and empty strings, or None with a reason if the file is unusable."""
    try:
        body = gzip.decompress((raw_dir / (record.path or "")).read_bytes())
    except FileNotFoundError:
        return None, "raw_file_missing", str(record.path)
    except (OSError, EOFError) as exc:
        return None, "unreadable_gzip", str(exc)[:300]
    if hashlib.sha256(body).hexdigest() != record.sha256:
        return None, "sha256_mismatch", f"manifest says {record.sha256}"
    return body, "", ""


def _gtfs_version(day: date, gtfs_dir: Path) -> GtfsVersion | None:
    try:
        return resolve_gtfs(day, gtfs_dir).version
    except GtfsVersionNotFoundError:
        return None


def replace_partition(root: Path, day: date, table: pa.Table) -> None:
    """Replace the partition of ``day`` with ``table``; an empty table removes it."""
    folder = root / f"service_date={day}"
    if table.num_rows == 0:
        shutil.rmtree(folder, ignore_errors=True)
        return
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, compression="zstd")
    write_atomic(folder / PART, sink.getvalue().to_pybytes())
    for other in folder.iterdir():
        if other.name != PART:
            other.unlink()


def _vehicle_message(row: dict[str, Any]) -> VehicleMessage | None:
    """Identify a vehicle message; a handover duplicate repeats it on a second trip."""
    if row["vehicle_id"] is None:
        return None
    return row["vehicle_id"], row["origin_ts"], row["lat"], row["lon"]


@dataclass
class _Tally:
    """Everything collected for one service date while its polls are read."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    rejects: list[dict[str, Any]] = field(default_factory=list)
    seen: dict[ObservationKey, tuple[int, int]] = field(default_factory=dict)
    first_trip: dict[VehicleMessage, str] = field(default_factory=dict)
    on_several_trips: set[VehicleMessage] = field(default_factory=set)
    not_ok: dict[str, int] = field(default_factory=dict)
    unknown_fields: set[str] = field(default_factory=set)
    polls_read: int = 0
    owned_ok: int = 0
    owned_rejected: int = 0
    owned_at_limit: int = 0
    features_parsed: int = 0
    repeats: int = 0
    repeats_changed: int = 0


def _reject_row(
    day: date, record: PollRecord, index: int | None, reason: str, detail: str
) -> dict[str, Any]:
    return {
        "service_date": day,
        "fetched_at": record.fetched_at,
        "raw_path": record.path,
        "raw_sha256": record.sha256,
        "feature_index": index,
        "reason": reason,
        "detail": detail,
    }


def _keep(
    row: dict[str, Any],
    day: date,
    record: PollRecord,
    at_limit: bool,
    gtfs: GtfsVersion | None,
    inputs: IngestInputs,
    tally: _Tally,
) -> None:
    """Add an observation to the day, or count it as a repeat of one already kept."""
    key = (row["trip_id"], row["trip_start_ts"], row["origin_ts"])
    content = hash(tuple(sorted(row.items())))
    known = tally.seen.get(key)
    if known is not None:
        index, first_content = known
        tally.rows[index]["seen_in_polls"] += 1
        tally.repeats += 1
        tally.repeats_changed += content != first_content
        return
    tally.seen[key] = (len(tally.rows), content)
    row.update(
        service_date=day,
        fetched_at=record.fetched_at,
        seen_in_polls=1,
        handover_duplicate=False,
        poll_at_limit=at_limit,
        gtfs_snapshot_date=None if gtfs is None else gtfs.snapshot_date,
        gtfs_sha256=None if gtfs is None else gtfs.sha256,
        parser_version=inputs.cfg.parser_version,
        raw_sha256=record.sha256,
        raw_path=record.path,
    )
    tally.rows.append(row)


def _index_messages(rows: list[dict[str, Any]], tally: _Tally) -> None:
    """Remember which trip first carried each vehicle message; note messages on 2+ trips."""
    for row in rows:
        message = _vehicle_message(row)
        if message is None:
            continue
        if tally.first_trip.setdefault(message, row["trip_id"]) != row["trip_id"]:
            tally.on_several_trips.add(message)


def _read_polls(
    day: date,
    records: list[PollRecord],
    gtfs: GtfsVersion | None,
    inputs: IngestInputs,
    tally: _Tally,
) -> None:
    """Parse the polls of the window and fill ``tally`` for ``day``."""
    start_min = inputs.cfg.service_day_start_min
    for record in records:
        owned = service_date_of(record.fetched_at, inputs.tz, start_min) == day
        if not record.ok:
            if owned:
                status = (
                    str(record.http_status)
                    if record.http_status is not None
                    else record.error or "no response"
                )
                tally.not_ok[status] = tally.not_ok.get(status, 0) + 1
            continue
        body, reason, detail = _read_body(inputs.raw_dir, record)
        if body is None:
            if owned:
                tally.owned_rejected += 1
                tally.rejects.append(_reject_row(day, record, None, reason, detail))
            continue
        check = tally.polls_read % inputs.cfg.schema_check_every == 0
        parsed = parse_poll(body, check_schema=check)
        tally.polls_read += 1
        tally.features_parsed += parsed.feature_count
        tally.unknown_fields |= parsed.extra_fields
        _index_messages(parsed.rows, tally)
        at_limit = parsed.feature_count == inputs.limit
        if owned:
            if any(r.feature_index is None for r in parsed.rejects):
                tally.owned_rejected += 1
            else:
                tally.owned_ok += 1
            tally.owned_at_limit += at_limit
            for r in parsed.rejects:
                tally.rejects.append(_reject_row(day, record, r.feature_index, r.reason, r.detail))
        for row in parsed.rows:
            if service_date_of(row["trip_start_ts"], inputs.tz, start_min) == day:
                _keep(row, day, record, at_limit, gtfs, inputs, tally)


def ingest_date(day: date, inputs: IngestInputs) -> dict[str, Any]:
    """Rebuild the positions, rejects and report of one service date; return the report."""
    started = time.perf_counter()
    cfg = inputs.cfg
    start = day_start_utc(day, inputs.tz, cfg.service_day_start_min)
    end = day_start_utc(day + timedelta(days=1), inputs.tz, cfg.service_day_start_min)
    window = (
        start - timedelta(hours=cfg.window_before_h),
        end + timedelta(hours=cfg.window_after_h),
    )
    records = read_manifests(inputs.manifest_dir, *window)
    gtfs = _gtfs_version(day, inputs.gtfs_dir)

    tally = _Tally()
    _read_polls(day, records, gtfs, inputs, tally)
    handovers = 0
    for row in tally.rows:
        message = _vehicle_message(row)
        row["handover_duplicate"] = message is not None and message in tally.on_several_trips
        handovers += row["handover_duplicate"]

    positions = pa.Table.from_pylist(tally.rows, schema=POSITIONS_SCHEMA).sort_by(
        [("trip_start_ts", "ascending"), ("trip_id", "ascending"), ("origin_ts", "ascending")]
    )
    rejects = pa.Table.from_pylist(tally.rejects, schema=REJECTS_SCHEMA)
    replace_partition(inputs.positions_dir, day, positions)
    replace_partition(inputs.rejects_dir, day, rejects)

    reasons: dict[str, int] = {}
    for r in tally.rejects:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    seconds = time.perf_counter() - started
    report: dict[str, Any] = {
        "service_date": day.isoformat(),
        "synthetic": inputs.synthetic,
        "parser_version": cfg.parser_version,
        "gtfs": None
        if gtfs is None
        else {"snapshot_date": gtfs.snapshot_date.isoformat(), "sha256": gtfs.sha256},
        "window": [t.isoformat() for t in window],
        "limit": inputs.limit,
        "owned_polls_ok": tally.owned_ok,
        "owned_polls_rejected": tally.owned_rejected,
        "owned_polls_not_ok_by_status": dict(sorted(tally.not_ok.items())),
        "owned_polls_at_limit": tally.owned_at_limit,
        "positions": positions.num_rows,
        "repeated_observations_dropped": tally.repeats,
        "repeats_with_changed_content": tally.repeats_changed,
        "handover_duplicate_rows": handovers,
        "rejects": len(tally.rejects),
        "reject_reasons": dict(sorted(reasons.items())),
        "unknown_fields": sorted(tally.unknown_fields),
        "benchmark": {
            "seconds": round(seconds, 2),
            "polls_parsed": tally.polls_read,
            "features_parsed": tally.features_parsed,
            "features_parsed_per_s": round(tally.features_parsed / seconds) if seconds else None,
            "positions_per_s": round(positions.num_rows / seconds) if seconds else None,
            "peak_memory_mb": peak_memory_mb(),
        },
    }
    write_atomic(
        inputs.reports_dir / f"{day}.json",
        (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return report


def ingest_dates(days: list[date], inputs: IngestInputs) -> list[dict[str, Any]]:
    """Ingest the given service dates in order and return their reports."""
    origin: dict[str, Any] = {"synthetic": inputs.synthetic}
    if inputs.synthetic:
        origin["note"] = "Built from a synthetic archive. Says nothing about real Prague."
    write_atomic(
        inputs.positions_dir.parent / "ORIGIN.json",
        (json.dumps(origin, indent=2) + "\n").encode("utf-8"),
    )
    return [ingest_date(day, inputs) for day in days]
