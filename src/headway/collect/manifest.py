"""Daily Parquet manifest of every request the collector made.

One file per UTC day, ``<manifest_dir>/YYYY-MM-DD.parquet``, with one row per HTTP attempt
(successful or not). Rows are buffered in memory and the day's file is rewritten atomically
on each flush, so a crash loses at most the rows since the last flush; the raw files
themselves are never affected.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from headway.fileio import write_atomic

MANIFEST_SCHEMA = pa.schema(
    [
        pa.field("fetched_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("attempt", pa.int16(), nullable=False),
        pa.field("http_status", pa.int16()),
        pa.field("sha256", pa.string()),
        pa.field("bytes", pa.int64()),
        pa.field("stored_bytes", pa.int64()),
        pa.field("latency_ms", pa.float64()),
        pa.field("path", pa.string()),
        pa.field("error", pa.string()),
    ]
)


@dataclass(frozen=True)
class PollRecord:
    """One HTTP attempt. ``path`` is relative to the raw directory."""

    fetched_at: datetime
    attempt: int
    http_status: int | None = None
    sha256: str | None = None
    bytes: int | None = None
    stored_bytes: int | None = None
    latency_ms: float | None = None
    path: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True if the attempt returned HTTP 200 and its body was stored."""
        return self.http_status == 200 and self.path is not None


def manifest_path(manifest_dir: Path, day: date) -> Path:
    """Return the manifest file for a UTC day."""
    return manifest_dir / f"{day:%Y-%m-%d}.parquet"


def read_manifest(path: Path) -> list[PollRecord]:
    """Read a manifest file back into records."""
    table = pq.read_table(path, schema=MANIFEST_SCHEMA)
    rows: list[dict[str, Any]] = table.to_pylist()
    return [PollRecord(**row) for row in rows]


class ManifestWriter:
    """Buffers records and writes them to the daily manifest files."""

    def __init__(self, manifest_dir: Path) -> None:
        """Create a writer for ``manifest_dir``."""
        self.manifest_dir = manifest_dir
        self._buffer: list[PollRecord] = []

    @property
    def pending(self) -> int:
        """Number of records not yet written."""
        return len(self._buffer)

    def add(self, record: PollRecord) -> None:
        """Buffer a record."""
        self._buffer.append(record)

    def flush(self) -> None:
        """Append buffered records to their day's file (atomic rewrite)."""
        by_day: dict[date, list[PollRecord]] = {}
        for record in self._buffer:
            by_day.setdefault(record.fetched_at.astimezone(UTC).date(), []).append(record)
        for day, records in sorted(by_day.items()):
            path = manifest_path(self.manifest_dir, day)
            existing = read_manifest(path) if path.exists() else []
            table = pa.Table.from_pylist(
                [asdict(r) for r in existing + records], schema=MANIFEST_SCHEMA
            )
            sink = pa.BufferOutputStream()
            pq.write_table(table, sink, compression="zstd")
            write_atomic(path, sink.getvalue().to_pybytes())
        self._buffer.clear()
