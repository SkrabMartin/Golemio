"""Load the tables of a resolved GTFS snapshot into Polars with explicit types.

Only standard GTFS columns get a declared type; any other column is kept as text. Times
(``arrival_time``, ``departure_time``) stay text because GTFS allows hours past 23; convert
them with :func:`headway.gtfs.timeconv.gtfs_time_to_utc`. Dates are parsed to ``Date``.
"""

import zipfile
from dataclasses import dataclass
from typing import Final

import polars as pl

from headway.gtfs.registry import ResolvedGtfs, sha256_file

_STR = pl.String
_INT8 = pl.Int8
_DATE = pl.Date

COLUMN_TYPES: Final[dict[str, dict[str, pl.DataType | type[pl.DataType]]]] = {
    "routes": {
        "route_id": _STR,
        "agency_id": _STR,
        "route_short_name": _STR,
        "route_long_name": _STR,
        "route_type": pl.Int32,
    },
    "trips": {
        "route_id": _STR,
        "service_id": _STR,
        "trip_id": _STR,
        "trip_headsign": _STR,
        "direction_id": _INT8,
        "block_id": _STR,
        "shape_id": _STR,
        "wheelchair_accessible": _INT8,
        "bikes_allowed": _INT8,
    },
    "stop_times": {
        "trip_id": _STR,
        "arrival_time": _STR,
        "departure_time": _STR,
        "stop_id": _STR,
        "stop_sequence": pl.Int32,
        "stop_headsign": _STR,
        "pickup_type": _INT8,
        "drop_off_type": _INT8,
        "shape_dist_traveled": pl.Float64,
        "timepoint": _INT8,
    },
    "stops": {
        "stop_id": _STR,
        "stop_code": _STR,
        "stop_name": _STR,
        "stop_lat": pl.Float64,
        "stop_lon": pl.Float64,
        "zone_id": _STR,
        "location_type": _INT8,
        "parent_station": _STR,
        "wheelchair_boarding": _INT8,
        "platform_code": _STR,
    },
    "shapes": {
        "shape_id": _STR,
        "shape_pt_lat": pl.Float64,
        "shape_pt_lon": pl.Float64,
        "shape_pt_sequence": pl.Int32,
        "shape_dist_traveled": pl.Float64,
    },
    "calendar": {
        "service_id": _STR,
        **dict.fromkeys(
            ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
            _INT8,
        ),
        "start_date": _DATE,
        "end_date": _DATE,
    },
    "calendar_dates": {"service_id": _STR, "date": _DATE, "exception_type": _INT8},
}
TABLES: Final = tuple(COLUMN_TYPES)
OPTIONAL_TABLES: Final = frozenset({"calendar", "calendar_dates", "shapes"})


class GtfsIntegrityError(RuntimeError):
    """The snapshot file no longer matches the hash recorded in the registry."""


@dataclass(frozen=True)
class GtfsFeed:
    """Tables of one GTFS version, tagged with the service date it was resolved for."""

    resolved: ResolvedGtfs
    tables: dict[str, pl.DataFrame]

    def __getitem__(self, name: str) -> pl.DataFrame:
        """Return a table by name, e.g. ``feed["stop_times"]``."""
        return self.tables[name]


def _read_table(archive: zipfile.ZipFile, name: str) -> pl.DataFrame:
    raw = pl.read_csv(archive.read(f"{name}.txt"), infer_schema=False, encoding="utf8")
    raw = raw.rename(lambda column: column.lstrip("﻿").strip())
    casts = []
    for column, dtype in COLUMN_TYPES[name].items():
        if column not in raw.columns:
            continue
        values = pl.col(column).str.strip_chars().replace("", None)
        if dtype == _DATE:
            casts.append(values.str.strptime(pl.Date, "%Y%m%d").alias(column))
        elif dtype != _STR:
            casts.append(values.cast(dtype, strict=True).alias(column))
    return raw.with_columns(casts) if casts else raw


def load_feed(resolved: ResolvedGtfs, tables: tuple[str, ...] = TABLES) -> GtfsFeed:
    """Load ``tables`` of the snapshot chosen by :func:`resolve_gtfs`.

    The zip is checked against the hash in the registry first.
    """
    if not isinstance(resolved, ResolvedGtfs):
        raise TypeError("load_feed() needs the result of resolve_gtfs()")
    unknown = set(tables) - set(COLUMN_TYPES)
    if unknown:
        raise ValueError(f"unknown GTFS tables: {sorted(unknown)}")
    if sha256_file(resolved.zip_path) != resolved.version.sha256:
        raise GtfsIntegrityError(f"{resolved.zip_path.name} does not match its registry hash")
    loaded: dict[str, pl.DataFrame] = {}
    with zipfile.ZipFile(resolved.zip_path) as archive:
        present = set(archive.namelist())
        for name in tables:
            if f"{name}.txt" in present:
                loaded[name] = _read_table(archive, name)
            elif name not in OPTIONAL_TABLES:
                raise ValueError(f"{resolved.zip_path.name} has no {name}.txt")
    return GtfsFeed(resolved, loaded)
