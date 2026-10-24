"""Write a synthetic timetable as a GTFS zip and register it like a real snapshot."""

import csv
import io
import zipfile
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

from headway.fileio import write_atomic
from headway.gtfs.registry import GtfsVersion, register_snapshot, sha256_file
from headway.gtfs.snapshot import snapshot_name
from headway.synth.network import Plane, Route
from headway.synth.timetable import TripPlan

AGENCIES = {
    "tram": ("SYNC", "SYNTH CITY TRANSIT"),
    "city_bus": ("SYNC", "SYNTH CITY TRANSIT"),
    "regional_bus": ("SYNR", "SYNTH REGIONAL"),
}


def _fmt_time(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _csv(header: list[str], rows: Iterable[Iterable[object]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def gtfs_files(
    routes: list[Route], trips: list[TripPlan], plane: Plane, start: date, end: date
) -> dict[str, str]:
    """Return the GTFS text files of one timetable version, valid from ``start`` to ``end``."""
    ymd = "%Y%m%d"
    stops = {s.stop_id: s for r in routes for d in r.directions for s in d.stops}
    files = {
        "agency.txt": _csv(
            ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
            sorted(
                {
                    (a, n, "https://example.invalid", "Europe/Prague", "cs")
                    for a, n in AGENCIES.values()
                }
            ),
        ),
        "feed_info.txt": _csv(
            [
                "feed_publisher_name",
                "feed_publisher_url",
                "feed_lang",
                "feed_start_date",
                "feed_end_date",
                "feed_version",
            ],
            [
                [
                    "Headway synthetic generator",
                    "https://github.com/SkrabMartin/headway",
                    "cs",
                    f"{start:{ymd}}",
                    f"{end:{ymd}}",
                    "synthetic",
                ]
            ],
        ),
        "routes.txt": _csv(
            ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
            [
                [
                    r.route_id,
                    AGENCIES[r.kind][0],
                    r.short_name,
                    f"Synthetic {r.kind} {r.short_name}",
                    r.route_type,
                ]
                for r in routes
            ],
        ),
        "stops.txt": _csv(
            [
                "stop_id",
                "stop_name",
                "stop_lat",
                "stop_lon",
                "location_type",
                "wheelchair_boarding",
            ],
            [
                [s.stop_id, s.name, *(f"{v:.6f}" for v in plane.to_latlon(s.x, s.y)), 0, 1]
                for s in sorted(stops.values(), key=lambda s: s.stop_id)
            ],
        ),
        "calendar.txt": _csv(
            [
                "service_id",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
                "start_date",
                "end_date",
            ],
            [
                ["WD", 1, 1, 1, 1, 1, 0, 0, f"{start:{ymd}}", f"{end:{ymd}}"],
                ["WE", 0, 0, 0, 0, 0, 1, 1, f"{start:{ymd}}", f"{end:{ymd}}"],
            ],
        ),
        "trips.txt": _csv(
            [
                "route_id",
                "service_id",
                "trip_id",
                "trip_headsign",
                "direction_id",
                "block_id",
                "shape_id",
                "wheelchair_accessible",
            ],
            [
                [
                    t.route.route_id,
                    t.service_id,
                    t.trip_id,
                    t.direction.stops[-1].name,
                    t.direction.direction_id,
                    t.block_id,
                    t.direction.shape_id,
                    1,
                ]
                for t in trips
            ],
        ),
        "stop_times.txt": _csv(
            [
                "trip_id",
                "arrival_time",
                "departure_time",
                "stop_id",
                "stop_sequence",
                "shape_dist_traveled",
            ],
            [
                [
                    t.trip_id,
                    _fmt_time(int(a)),
                    _fmt_time(int(d)),
                    s.stop_id,
                    i + 1,
                    f"{dist / 1000:.3f}",
                ]
                for t in trips
                for i, (a, d, s, dist) in enumerate(
                    zip(t.arr_s, t.dep_s, t.direction.stops, t.direction.stop_dist, strict=True)
                )
            ],
        ),
        "shapes.txt": _csv(
            [
                "shape_id",
                "shape_pt_lat",
                "shape_pt_lon",
                "shape_pt_sequence",
                "shape_dist_traveled",
            ],
            [
                [
                    d.shape_id,
                    *(f"{v:.6f}" for v in plane.to_latlon(float(x), float(y))),
                    i + 1,
                    f"{dist / 1000:.3f}",
                ]
                for r in routes
                for d in r.directions
                for i, ((x, y), dist) in enumerate(zip(d.path.xy, d.path.dist, strict=True))
            ],
        ),
    }
    return files


def write_gtfs_snapshot(gtfs_dir: Path, snapshot_date: date, files: dict[str, str]) -> GtfsVersion:
    """Zip ``files`` deterministically, store them as a snapshot and register it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            archive.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), files[name])
    data = buffer.getvalue()
    tmp = gtfs_dir / "synthetic.zip.part"
    write_atomic(tmp, data)
    target = gtfs_dir / snapshot_name(snapshot_date, sha256_file(tmp))
    tmp.replace(target)
    return register_snapshot(gtfs_dir, target)


def period_end(start: date, days: int) -> date:
    """Last service date of a period of ``days`` days starting at ``start``."""
    return start + timedelta(days=days - 1)
