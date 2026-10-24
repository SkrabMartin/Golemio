"""Static GTFS snapshots, version registry and time conversion.

Archives daily snapshots of the PID static GTFS feed keyed by content hash, maintains a
registry that maps every service date to the exact feed version valid on that date, and
loads the required tables with explicit types. It also owns the single, tested function
that converts GTFS schedule times (including times past 24:00) to UTC across daylight
saving changes.

Schedule data is reachable only through a resolved version::

    resolved = resolve_gtfs(service_date, gtfs_dir)
    feed = load_feed(resolved)
"""

from headway.gtfs.loader import GtfsFeed, GtfsIntegrityError, load_feed
from headway.gtfs.registry import (
    GtfsVersion,
    GtfsVersionNotFoundError,
    ResolvedGtfs,
    read_registry,
    rebuild_registry,
    resolve_gtfs,
)
from headway.gtfs.snapshot import SnapshotResult, snapshot_gtfs
from headway.gtfs.timeconv import gtfs_time_to_utc, parse_gtfs_time

__all__ = [
    "GtfsFeed",
    "GtfsIntegrityError",
    "GtfsVersion",
    "GtfsVersionNotFoundError",
    "ResolvedGtfs",
    "SnapshotResult",
    "gtfs_time_to_utc",
    "load_feed",
    "parse_gtfs_time",
    "read_registry",
    "rebuild_registry",
    "resolve_gtfs",
    "snapshot_gtfs",
]
