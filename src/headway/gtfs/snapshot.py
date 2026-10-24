"""Daily snapshots of the static PID GTFS feed, stored by content hash."""

import contextlib
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from headway.fileio import tmp_path_for
from headway.gtfs.registry import GtfsVersion, read_registry, register_snapshot

log = logging.getLogger(__name__)
USER_AGENT = "headway-gtfs-snapshot (+https://github.com/SkrabMartin/headway)"


@dataclass(frozen=True)
class SnapshotResult:
    """Outcome of one snapshot run."""

    sha256: str
    bytes: int
    stored: bool  # False if the content equals the latest stored snapshot
    version: GtfsVersion | None


def snapshot_name(day: date, sha256: str) -> str:
    """Return ``PID_GTFS_<YYYY-MM-DD>_<sha12>.zip``."""
    return f"PID_GTFS_{day:%Y-%m-%d}_{sha256[:12]}.zip"


def snapshot_gtfs(
    url: str,
    gtfs_dir: Path,
    *,
    timeout_s: float,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> SnapshotResult:
    """Download the feed and store it unless it equals the latest snapshot.

    The body is streamed to a temporary file while hashing, so memory use stays small; the
    file is renamed into place only after the download completed and the hash is known.
    """
    now = now or datetime.now(UTC)
    gtfs_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(gtfs_dir / "PID_GTFS_download.zip")
    digest = hashlib.sha256()
    size = 0
    own_client = client is None
    http = client or httpx.Client(
        timeout=timeout_s, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
    try:
        with http.stream("GET", url) as response:
            response.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        sha = digest.hexdigest()

        latest = read_registry(gtfs_dir)
        if latest and max(latest, key=lambda v: v.snapshot_date).sha256 == sha:
            log.info("gtfs unchanged", extra={"sha256": sha, "bytes": size})
            return SnapshotResult(sha, size, stored=False, version=None)

        target = gtfs_dir / snapshot_name(now.astimezone(UTC).date(), sha)
        os.replace(tmp, target)
        version = register_snapshot(gtfs_dir, target)
        log.info(
            "gtfs stored",
            extra={
                "path": version.path,
                "bytes": size,
                "feed_start_date": str(version.feed_start_date),
                "feed_end_date": str(version.feed_end_date),
            },
        )
        return SnapshotResult(sha, size, stored=True, version=version)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        if own_client:
            http.close()
