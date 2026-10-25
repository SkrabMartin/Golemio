"""Reconstruction of stop events for one service date, from positions and the timetable.

Reads the positions that ingest wrote and the GTFS version of the day, and writes, per service
date: the events, the trips that could not be reconstructed (each with a reason), the
comparison with the delay the API reports, and a report with the counts.
"""

import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pyarrow as pa

from headway.audit.geometry import to_metric
from headway.audit.positions import build_timetable
from headway.config import Settings
from headway.events.compare import (
    event_time_and_delay,
    nearest_reported,
    stop_position,
    summarize,
)
from headway.events.config import EventsConfig
from headway.events.project import FloatArray, cumulative_length, project_along
from headway.events.reconstruct import StopEvent, reconstruct_events
from headway.events.schema import COMPARE_SCHEMA, EVENTS_SCHEMA, EXCLUSIONS_SCHEMA
from headway.fileio import write_atomic
from headway.gtfs import GtfsVersionNotFoundError, load_feed, resolve_gtfs
from headway.gtfs.timeconv import service_day_reference
from headway.ingest.pipeline import IngestInputs, inputs_for, replace_partition
from headway.ingest.resources import peak_memory_mb


@dataclass(frozen=True)
class EventsInputs:
    """Where the reconstruction reads from and writes to."""

    ingest: IngestInputs
    events_dir: Path
    exclusions_dir: Path
    compare_dir: Path
    reports_dir: Path
    cfg: EventsConfig


def events_inputs_for(root: Path, settings: Settings, cfg: EventsConfig) -> EventsInputs:
    """Return the reconstruction inputs of the archive rooted at ``root``."""
    ingest = inputs_for(root, settings)
    data = ingest.positions_dir.parent
    return EventsInputs(
        ingest=ingest,
        events_dir=data / "events",
        exclusions_dir=data / "event_exclusions",
        compare_dir=data / "event_comparison",
        reports_dir=data / "event_reports",
        cfg=cfg,
    )


@dataclass(frozen=True)
class _Stops:
    """The scheduled stops of one trip, in order."""

    sequence: list[int]
    stop_id: list[str]
    dist_m: FloatArray  # NaN where the timetable does not give it
    sched_arr: FloatArray  # epoch seconds, NaN where unknown
    sched_dep: FloatArray


def _seconds(column: str) -> pl.Expr:
    """GTFS ``H:MM:SS`` (hours may pass 24) as seconds; null if the text is not a time."""
    parts = pl.col(column).str.split(":")

    def part(i: int) -> pl.Expr:
        return parts.list.get(i, null_on_oob=True).cast(pl.Int64, strict=False)

    return part(0) * 3600 + part(1) * 60 + part(2)


def _stops_by_trip(stop_times: pl.DataFrame, day: date, tz: str) -> dict[str, _Stops]:
    """Group the stop times by trip, with scheduled times as UTC epoch seconds.

    The day starts at "noon minus 12 h" and times may run past 24:00, so every time goes
    through the single tested reference of ``service_day_reference``.
    """
    reference = service_day_reference(day, tz).timestamp()
    frame = stop_times.with_columns(
        (_seconds("arrival_time") + reference).cast(pl.Float64).alias("sched_arr"),
        (_seconds("departure_time") + reference).cast(pl.Float64).alias("sched_dep"),
    ).sort("trip_id", "stop_sequence")
    out: dict[str, _Stops] = {}
    for key, part in frame.partition_by("trip_id", as_dict=True).items():
        out[str(key[0])] = _Stops(
            part["stop_sequence"].to_list(),
            part["stop_id"].to_list(),
            part["shape_dist_traveled"].cast(pl.Float64).fill_null(float("nan")).to_numpy()
            * 1000.0,
            part["sched_arr"].fill_null(float("nan")).to_numpy(),
            part["sched_dep"].fill_null(float("nan")).to_numpy(),
        )
    return out


def _stop_distances(
    stops: _Stops,
    line: FloatArray,
    cumulative: FloatArray,
    stop_xy: dict[str, FloatArray],
    cfg: EventsConfig,
) -> tuple[FloatArray | None, str]:
    """Return the distance of each stop along the shape, or ``(None, reason)``.

    The timetable's ``shape_dist_traveled`` is used when it is complete and never decreases.
    Otherwise the stops are projected onto the shape, which is only trusted if every stop is
    near it and the order of the stops is kept (a loop that passes a place twice fails this
    and is excluded rather than guessed).
    """
    given = stops.dist_m
    if np.all(np.isfinite(given)) and given[-1] > given[0] and np.all(np.diff(given) >= 0):
        return given, ""
    if not all(s in stop_xy for s in stops.stop_id):
        return None, "stop_distances_unusable: stop positions missing"
    points = np.array([stop_xy[s] for s in stops.stop_id])
    along, offset = project_along(
        points, line, cumulative, None, cfg.projection.window_m, cfg.stops.projected_stop_offset_m
    )
    if np.any(offset > cfg.stops.projected_stop_offset_m):
        return None, "stop_distances_unusable: a stop is far from the shape"
    if np.any(np.diff(along) < 0):
        return None, "stop_distances_unusable: projection does not keep the stop order (loop?)"
    return along, ""


def _utc(seconds: float | None) -> datetime | None:
    return (
        None
        if seconds is None or not np.isfinite(seconds)
        else datetime.fromtimestamp(seconds, UTC)
    )


def _delay(actual: float | None, scheduled: float) -> float | None:
    if actual is None or not np.isfinite(scheduled):
        return None
    return actual - scheduled


def ingest_stop_xy(stops: pl.DataFrame, wanted: set[str]) -> dict[str, FloatArray]:
    """Return ``{stop_id: [x, y]}`` in metres for the stops in ``wanted``."""
    part = stops.filter(pl.col("stop_id").is_in(wanted)).drop_nulls(["stop_lat", "stop_lon"])
    xy = to_metric(part["stop_lon"].to_numpy(), part["stop_lat"].to_numpy())
    return {sid: xy[i] for i, sid in enumerate(part["stop_id"].to_list())}


def reconstruct_date(day: date, inputs: EventsInputs) -> dict[str, Any]:
    """Reconstruct the events of one service date; write its files and return the report."""
    started = time.perf_counter()
    ingest, cfg = inputs.ingest, inputs.cfg
    path = ingest.positions_dir / f"service_date={day}" / "part-0.parquet"
    notes: list[str] = []
    events: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    compare: list[dict[str, Any]] = []
    counters = {"trips": 0, "reconstructed": 0, "positions_used": 0, "positions_off_shape": 0}
    scheduled_stops = 0

    df = pl.read_parquet(path) if path.exists() else pl.DataFrame()
    if df.height:
        df = df.filter(~pl.col("handover_duplicate") & pl.col("lat").is_not_null())
    gtfs_sha: str | None = None
    if df.height:
        try:
            resolved = resolve_gtfs(day, ingest.gtfs_dir)
        except GtfsVersionNotFoundError:
            resolved = None
            notes.append("No timetable (GTFS) covers this date, so no trip could be reconstructed.")
        if resolved is not None:
            gtfs_sha = resolved.version.sha256
            feed = load_feed(resolved, tables=("trips", "stop_times", "stops", "shapes"))
            wanted = set(df["trip_id"].unique().to_list())
            timetable = build_timetable(feed["trips"], feed["shapes"], wanted)
            stop_times = feed["stop_times"].filter(pl.col("trip_id").is_in(wanted))
            stops_by_trip = _stops_by_trip(stop_times, day, ingest.tz)
            stop_xy = ingest_stop_xy(feed["stops"], set(stop_times["stop_id"].unique().to_list()))
            direction = dict(
                zip(
                    feed["trips"]["trip_id"].to_list(),
                    feed["trips"]["direction_id"].to_list(),
                    strict=True,
                )
            )
            for key, trip in df.partition_by(
                ["trip_id", "trip_start_ts"], maintain_order=True, as_dict=True
            ).items():
                counters["trips"] += 1
                trip_id, start = str(key[0]), key[1]

                def exclude(
                    reason: str, detail: str = "", _id: str = trip_id, _s: Any = start
                ) -> None:
                    exclusions.append(
                        {
                            "service_date": day,
                            "trip_id": _id,
                            "trip_start_ts": _s,
                            "reason": reason,
                            "detail": detail or reason,
                        }
                    )

                stops = stops_by_trip.get(trip_id)
                shape_id = timetable.trip_shape.get(trip_id)
                line = timetable.shapes.get(shape_id) if shape_id is not None else None
                if trip_id not in timetable.trip_ids or stops is None:
                    exclude(
                        "not_in_timetable", "trip has no stop times in the timetable of the day"
                    )
                    continue
                if line is None or len(line) < 2:
                    exclude("no_shape", "the trip has no usable shape")
                    continue
                cumulative = cumulative_length(line)
                stop_d, why = _stop_distances(stops, line, cumulative, stop_xy, cfg)
                if stop_d is None:
                    exclude("stop_distances_unusable", why)
                    continue

                metric = to_metric(trip["lon"].to_numpy(), trip["lat"].to_numpy())
                hints = (
                    trip["shape_dist_km"].cast(pl.Float64).fill_null(float("nan")).to_numpy()
                    * 1000.0
                )
                along, offset = project_along(
                    metric,
                    line,
                    cumulative,
                    hints,
                    cfg.projection.window_m,
                    cfg.projection.max_offset_m,
                )
                seconds = trip["origin_ts"].dt.epoch("us").to_numpy() / 1e6
                reported = (
                    trip["reported_delay_s"].cast(pl.Float64).fill_null(float("nan")).to_numpy()
                )
                on_route = offset <= cfg.projection.max_offset_m
                counters["positions_off_shape"] += int((~on_route).sum())
                keep = on_route & np.r_[True, np.diff(seconds) > 0]
                if int(keep.sum()) < cfg.trip.min_positions:
                    exclude("too_few_positions", f"{int(keep.sum())} usable positions")
                    continue
                counters["reconstructed"] += 1
                counters["positions_used"] += int(keep.sum())
                found = reconstruct_events(
                    seconds[keep], along[keep], stop_d, cfg.stops.radius_m, cfg.plateau.eps_m
                )
                scheduled_stops += len(stop_d)
                route_type = int(trip["route_type"][0])
                route_id = str(trip["route_id"][0])
                vehicle = trip["vehicle_id"][0]
                for event in found:
                    row, compared = _event_rows(
                        day,
                        trip_id,
                        start,
                        route_id,
                        route_type,
                        direction.get(trip_id),
                        vehicle,
                        stops,
                        stop_d,
                        event,
                        gtfs_sha,
                    )
                    events.append(row)
                    if compared is not None:
                        at, delay = compared
                        match = nearest_reported(seconds, reported, at, cfg.compare.max_dt_s)
                        if match is not None:
                            compare.append(
                                {
                                    "service_date": day,
                                    "trip_id": trip_id,
                                    "route_id": route_id,
                                    "route_type": route_type,
                                    "stop_sequence": stops.sequence[event.stop_index],
                                    "stop_position": stop_position(event.stop_index, len(stop_d)),
                                    "event_ts": _utc(at),
                                    "reconstructed_delay_s": delay,
                                    "reported_delay_s": match[0],
                                    "diff_s": match[0] - delay,
                                    "dt_s": match[1],
                                    "uncert_s": row["uncert_s"],
                                }
                            )
    else:
        notes.append("No positions were ingested for this date.")

    events_table = pa.Table.from_pylist(events, schema=EVENTS_SCHEMA)
    exclusions_table = pa.Table.from_pylist(exclusions, schema=EXCLUSIONS_SCHEMA)
    compare_table = pa.Table.from_pylist(compare, schema=COMPARE_SCHEMA)
    replace_partition(inputs.events_dir, day, events_table)
    replace_partition(inputs.exclusions_dir, day, exclusions_table)
    replace_partition(inputs.compare_dir, day, compare_table)

    reasons: dict[str, int] = {}
    for r in exclusions:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    methods: dict[str, int] = {}
    for e in events:
        methods[e["method"]] = methods.get(e["method"], 0) + 1
    report: dict[str, Any] = {
        "service_date": day.isoformat(),
        "synthetic": ingest.synthetic,
        "gtfs_sha256": gtfs_sha,
        "trips_with_positions": counters["trips"],
        "trips_reconstructed": counters["reconstructed"],
        "trips_excluded": len(exclusions),
        "exclusion_reasons": dict(sorted(reasons.items())),
        "positions_used": counters["positions_used"],
        "positions_off_shape_dropped": counters["positions_off_shape"],
        "events": len(events),
        "events_by_method": dict(sorted(methods.items())),
        "stops_of_reconstructed_trips": scheduled_stops,
        "event_coverage": round(len(events) / scheduled_stops, 4) if scheduled_stops else None,
        "comparison": summarize(pl.from_arrow(compare_table)),  # type: ignore[arg-type]
        "thresholds": cfg.model_dump(),
        "notes": notes,
        "seconds": round(time.perf_counter() - started, 2),
        "peak_memory_mb": peak_memory_mb(),
    }
    write_atomic(
        inputs.reports_dir / f"{day}.json",
        (json.dumps(report, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8"),
    )
    return report


def _event_rows(
    day: date,
    trip_id: str,
    start: Any,
    route_id: str,
    route_type: int,
    direction_id: int | None,
    vehicle: str | None,
    stops: _Stops,
    stop_d: FloatArray,
    event: StopEvent,
    gtfs_sha: str | None,
) -> tuple[dict[str, Any], tuple[float, float] | None]:
    """Build the output row of an event and the (time, delay) to compare with the API."""
    i = event.stop_index
    sched_arr, sched_dep = float(stops.sched_arr[i]), float(stops.sched_dep[i])
    arr_delay = _delay(event.arr, sched_arr)
    dep_delay = _delay(event.dep, sched_dep)
    delay = arr_delay if arr_delay is not None else dep_delay
    row = {
        "service_date": day,
        "trip_id": trip_id,
        "trip_start_ts": start,
        "route_id": route_id,
        "route_type": route_type,
        "direction_id": direction_id,
        "vehicle_id": vehicle,
        "stop_id": stops.stop_id[i],
        "stop_sequence": stops.sequence[i],
        "stop_dist_m": float(stop_d[i]),
        "arr_ts": _utc(event.arr),
        "dep_ts": _utc(event.dep),
        "sched_arr_ts": _utc(sched_arr),
        "sched_dep_ts": _utc(sched_dep),
        "arr_delay_s": arr_delay,
        "dep_delay_s": dep_delay,
        "delay_s": delay,
        "uncert_s": event.arr_uncert if event.arr_uncert is not None else event.dep_uncert,
        "dep_uncert_s": event.dep_uncert,
        "n_positions": event.n_positions,
        "method": event.method,
        "gtfs_sha256": gtfs_sha,
    }
    return row, event_time_and_delay(event, arr_delay, dep_delay)


def reconstruct_dates(days: list[date], inputs: EventsInputs) -> list[dict[str, Any]]:
    """Reconstruct the given service dates in order and return their reports."""
    return [reconstruct_date(day, inputs) for day in days]
