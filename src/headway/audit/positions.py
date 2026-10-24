"""Findings about positions, trips and vehicles of one service day."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import numpy.typing as npt
import polars as pl

from headway.audit.config import PositionThresholds, TripThresholds
from headway.audit.findings import Finding
from headway.audit.geometry import FloatArray, distance_to_polyline, to_metric

BoolArray = npt.NDArray[np.bool_]
Span = tuple[datetime, datetime]


@dataclass(frozen=True)
class Timetable:
    """What the audit needs from the GTFS version of the day."""

    trip_ids: frozenset[str]
    trip_shape: dict[str, str | None]
    shapes: dict[str, FloatArray]  # shape_id -> polyline in metres


@dataclass
class PositionStats:
    """Numbers behind the position lines of the summary."""

    positions: int = 0
    trips: int = 0
    vehicles: int = 0
    no_coordinates: int = 0
    age_p50_s: float | None = None
    age_p90_s: float | None = None
    age_p99_s: float | None = None
    age_max_s: float | None = None
    stale: int = 0
    shape_checked: int = 0
    on_shape: int = 0
    trips_without_shape: int = 0
    timetable_available: bool = False
    by_route_type: dict[int, int] = field(default_factory=dict)

    @property
    def fresh_share(self) -> float | None:
        """Share of positions that were not stale."""
        return None if self.positions == 0 else 1 - self.stale / self.positions

    @property
    def on_shape_share(self) -> float | None:
        """Share of checked positions that lie on the trip shape."""
        return None if self.shape_checked == 0 else self.on_shape / self.shape_checked


def build_timetable(
    trips: pl.DataFrame, shapes: pl.DataFrame | None, wanted_trips: set[str]
) -> Timetable:
    """Build the lookup for the trips in ``wanted_trips`` from the GTFS tables."""
    rows = trips.filter(pl.col("trip_id").is_in(wanted_trips)).select("trip_id", "shape_id")
    trip_shape = dict(zip(rows["trip_id"].to_list(), rows["shape_id"].to_list(), strict=True))
    polylines: dict[str, FloatArray] = {}
    if shapes is not None:
        needed = {s for s in trip_shape.values() if s is not None}
        for (shape_id,), part in (
            shapes.filter(pl.col("shape_id").is_in(needed))
            .partition_by("shape_id", as_dict=True)
            .items()
        ):
            part = part.sort("shape_pt_sequence")
            polylines[str(shape_id)] = to_metric(
                part["shape_pt_lon"].to_numpy(), part["shape_pt_lat"].to_numpy()
            )
    return Timetable(frozenset(trips["trip_id"].to_list()), trip_shape, polylines)


def runs(mask: BoolArray) -> list[tuple[int, int]]:
    """Return the first and last index of every run of True values."""
    found: list[tuple[int, int]] = []
    start: int | None = None
    for i, value in enumerate(mask.tolist()):
        if value and start is None:
            start = i
        elif not value and start is not None:
            found.append((start, i - 1))
            start = None
    if start is not None:
        found.append((start, len(mask) - 1))
    return found


def _seconds(values: list[datetime]) -> FloatArray:
    return np.array([v.timestamp() for v in values], dtype=np.float64)


def _covered_s(start: datetime, end: datetime, spans: list[Span]) -> float:
    """Seconds of ``[start, end]`` that lie inside at least one of ``spans``."""
    pieces = sorted((max(a, start), min(b, end)) for a, b in spans if a < end and b > start)
    total, cursor = 0.0, None
    for a, b in pieces:
        if cursor is None or a > cursor:
            total += (b - a).total_seconds()
            cursor = b
        elif b > cursor:
            total += (b - cursor).total_seconds()
            cursor = b
    return total


def _hides_rest_of_trip(last: datetime, rest_s: float, spans: list[Span], slack_s: float) -> bool:
    """True if one collector problem could have hidden everything after ``last``.

    The problem must still be going on after ``last``, start within ``slack_s`` of it, and
    last (give or take ``slack_s``) as long as the rest of the trip would have taken: the
    end of the trip is only seen at the next poll.
    """
    slack = timedelta(seconds=slack_s)
    return any(
        b > last  # still going on after the last position
        and a <= last + slack
        and b + slack >= last + timedelta(seconds=rest_s)
        for a, b in spans
    )


def position_findings(
    df: pl.DataFrame,
    pos: PositionThresholds,
    trips_th: TripThresholds,
    timetable: Timetable | None,
    explained: list[Span],
    data_end: datetime | None,
) -> tuple[list[Finding], PositionStats]:
    """Audit the positions of one service day.

    ``explained`` are time spans in which the collector itself had a problem; a silent
    trip inside one of them is not reported as silent. ``data_end`` is the time of the
    last successful poll of the day.
    """
    findings: list[Finding] = []
    stats = PositionStats(timetable_available=timetable is not None)
    if df.height == 0:
        return findings, stats

    ages = (df["fetched_at"].dt.epoch("us") - df["origin_ts"].dt.epoch("us")).to_numpy() / 1e6
    stats.positions = df.height
    stats.age_p50_s, stats.age_p90_s, stats.age_p99_s = (
        float(q) for q in np.percentile(ages, [50, 90, 99])
    )
    stats.age_max_s = float(ages.max())
    stats.stale = int((ages > pos.stale_age_s).sum())
    stats.no_coordinates = df.filter(pl.col("lat").is_null() | pl.col("lon").is_null()).height
    stats.vehicles = df["vehicle_id"].drop_nulls().n_unique()
    stats.by_route_type = {int(k): int(v) for k, v in df.group_by("route_type").len().iter_rows()}

    for key, trip in df.partition_by(
        ["trip_id", "trip_start_ts"], maintain_order=True, as_dict=True
    ).items():
        findings += _trip_findings(
            str(key[0]), trip, pos, trips_th, timetable, explained, data_end, stats
        )
        stats.trips += 1
    findings += _vehicle_findings(df, trips_th)
    return findings, stats


def _trip_findings(
    trip_id: str,
    trip: pl.DataFrame,
    pos: PositionThresholds,
    trips_th: TripThresholds,
    timetable: Timetable | None,
    explained: list[Span],
    data_end: datetime | None,
    stats: PositionStats,
) -> list[Finding]:
    # A handover duplicate carries another trip's timestamp and position. It is reported as
    # vehicle_on_two_trips and must not count as a message of this trip.
    trip = trip.filter(~pl.col("handover_duplicate"))
    if trip.height == 0:
        return []
    out: list[Finding] = []
    origin = trip["origin_ts"].to_list()
    origin_s = _seconds(origin)
    route_type = int(trip["route_type"][0])
    label = f"line {trip['route_short_name'][0]}, vehicle {trip['vehicle_id'][0] or 'unknown'}"

    if timetable is not None and trip_id not in timetable.trip_ids:
        out.append(
            Finding(
                "unknown_trip",
                "trip",
                trip_id,
                origin[0],
                origin[-1],
                None,
                f"{label}: trip id not in the timetable of the day",
            )
        )

    # Stale positions: runs of consecutive observations that arrived late.
    ages = (trip["fetched_at"].dt.epoch("us") - trip["origin_ts"].dt.epoch("us")).to_numpy() / 1e6
    for i, j in runs(ages > pos.stale_age_s):
        out.append(
            Finding(
                "stale_position",
                "trip",
                trip_id,
                origin[i],
                origin[j],
                float(ages[i : j + 1].max()),
                f"{label}: {j - i + 1} position(s) arrived more than {pos.stale_age_s:.0f} s "
                f"late, worst {ages[i : j + 1].max():.0f} s",
            )
        )

    # Silent: a long time without a new position. Time that a collector problem covers
    # does not count, so a short outage cannot excuse a long silence.
    for g in np.flatnonzero(np.diff(origin_s) > trips_th.silent_min * 60):
        a, b = origin[int(g)], origin[int(g) + 1]
        total = (b - a).total_seconds()
        covered = _covered_s(a, b, explained)
        if total - covered > trips_th.silent_min * 60:
            note = f" ({covered / 60:.1f} min of it during collector problems)" if covered else ""
            out.append(
                Finding(
                    "silent_trip",
                    "trip",
                    trip_id,
                    a,
                    b,
                    total,
                    f"{label}: no new position for {total / 60:.1f} min{note}",
                )
            )

    valid = trip["lat"].is_not_null().to_numpy() & trip["lon"].is_not_null().to_numpy()
    if not valid.any():
        return out
    idx = np.flatnonzero(valid)
    xy = to_metric(trip["lon"].to_numpy()[idx], trip["lat"].to_numpy()[idx])

    # GPS jumps: an impossible speed between two positions, or a spike (a position far from
    # the line between its two neighbours, which agree with each other).
    out += _jump_findings(trip_id, label, origin, origin_s[idx], idx, xy, route_type, pos)

    # Off the route: distance from the trip shape.
    if timetable is not None and trip_id in timetable.trip_ids:
        shape_id = timetable.trip_shape.get(trip_id)
        polyline = timetable.shapes.get(shape_id) if shape_id is not None else None
        if polyline is None:
            stats.trips_without_shape += 1
        else:
            short = _stopped_short(trip_id, label, trip, polyline, data_end, trips_th, explained)
            if short is not None:
                out.append(short)
            dist = distance_to_polyline(xy, polyline)
            stats.shape_checked += len(dist)
            stats.on_shape += int((dist <= pos.off_shape_m).sum())
            for i, j in runs(dist > pos.off_shape_m):
                out.append(
                    Finding(
                        "off_shape",
                        "trip",
                        trip_id,
                        origin[int(idx[i])],
                        origin[int(idx[j])],
                        float(dist[i : j + 1].max()),
                        f"{label}: {j - i + 1} position(s) more than {pos.off_shape_m:.0f} m "
                        f"from the route, up to {dist[i : j + 1].max():.0f} m",
                    )
                )
    return out


def _jump_findings(
    trip_id: str,
    label: str,
    origin: list[datetime],
    seconds: FloatArray,
    idx: npt.NDArray[np.intp],
    xy: FloatArray,
    route_type: int,
    pos: PositionThresholds,
) -> list[Finding]:
    """Find GPS jumps among the positions of one trip.

    The speed rule alone misses a jump of a few hundred metres when reports are 30 s apart,
    which is below the speed limit; the spike rule catches those. ``value`` is the size of
    the jump in metres. Overlapping hits are merged into one finding.
    """
    if len(idx) < 2:
        return []
    limit = pos.jump_speed(route_type)
    hits: list[tuple[int, int, float, str]] = []  # first, last (in idx space), metres, text

    dt = np.diff(seconds)
    step = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    speed = np.divide(step, dt, out=np.zeros_like(step), where=dt >= pos.jump_min_dt_s)
    for i, j in runs(speed > limit):
        worst = int(i + np.argmax(speed[i : j + 1]))
        hits.append(
            (
                i,
                j + 1,
                float(step[worst]),
                f"moved {step[worst]:.0f} m in {dt[worst]:.0f} s "
                f"({speed[worst]:.0f} m/s, limit {limit:.0f} m/s)",
            )
        )

    if len(idx) > 2:
        chord = xy[2:] - xy[:-2]
        length2 = (chord**2).sum(axis=1)
        along = np.divide(
            ((xy[1:-1] - xy[:-2]) * chord).sum(axis=1),
            length2,
            out=np.zeros_like(length2),
            where=length2 > 0,
        )
        nearest = xy[:-2] + np.clip(along, 0.0, 1.0)[:, None] * chord
        away = np.linalg.norm(xy[1:-1] - nearest, axis=1)
        span = seconds[2:] - seconds[:-2]
        neighbours_agree = np.sqrt(length2) <= limit * np.maximum(span, pos.jump_min_dt_s)
        # The line between the neighbours only means something if they are close in time.
        close = span <= pos.jump_spike_span_s
        for k in np.flatnonzero((away > pos.jump_spike_m) & neighbours_agree & close):
            hits.append(
                (
                    int(k),
                    int(k) + 2,
                    float(away[k]),
                    f"jumped {away[k]:.0f} m away from the line between its neighbours "
                    f"and back (limit {pos.jump_spike_m:.0f} m)",
                )
            )

    merged: list[tuple[int, int, float, str]] = []
    for hit in sorted(hits):
        if merged and hit[0] <= merged[-1][1]:
            last = merged[-1]
            best = hit if hit[2] > last[2] else last
            merged[-1] = (last[0], max(last[1], hit[1]), best[2], best[3])
        else:
            merged.append(hit)
    return [
        Finding(
            "gps_jump",
            "trip",
            trip_id,
            origin[int(idx[a])],
            origin[int(idx[b])],
            metres,
            f"{label}: {text}",
        )
        for a, b, metres, text in merged
    ]


def _stopped_short(
    trip_id: str,
    label: str,
    trip: pl.DataFrame,
    polyline: FloatArray,
    data_end: datetime | None,
    trips_th: TripThresholds,
    explained: list[Span],
) -> Finding | None:
    """Return a finding if the trip stopped reporting well before the end of its route.

    That happens when a silent period runs to the end of the trip, so there is no gap
    between positions to see. ASSUMPTION: ``shape_dist_traveled`` is measured along the
    same shape as the GTFS polyline, so the rest of the route is the polyline length minus it.
    """
    travelled = trip.select("origin_ts", "shape_dist_km").drop_nulls()
    if travelled.height == 0 or data_end is None or len(polyline) < 2:
        return None
    length = float(np.linalg.norm(np.diff(polyline, axis=0), axis=1).sum())
    remaining = length - float(travelled["shape_dist_km"][-1]) * 1000
    last = travelled["origin_ts"][-1]
    silent_s = (data_end - last).total_seconds()
    if remaining <= trips_th.early_end_m or silent_s <= trips_th.silent_min * 60:
        return None
    # A collector problem that starts soon after the last position and lasts as long as the
    # rest of the route could take at a generous average speed would hide the missing tail.
    rest_s = remaining / trips_th.explain_speed_mps
    if _hides_rest_of_trip(last, rest_s, explained, trips_th.explain_slack_s):
        return None
    return Finding(
        "silent_trip",
        "trip",
        trip_id,
        last,
        data_end,
        silent_s,
        f"{label}: last position {remaining / 1000:.1f} km before the end of the route, "
        "then nothing for the rest of the day",
    )


def _vehicle_findings(df: pl.DataFrame, trips_th: TripThresholds) -> list[Finding]:
    """One vehicle on two trips: the same message twice, or overlapping trip spans."""
    out: list[Finding] = []
    seen: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    flagged = df.filter(pl.col("handover_duplicate"))
    for vehicle, origin, trip_id in flagged.select(
        "vehicle_id", "origin_ts", "trip_id"
    ).iter_rows():
        seen[(vehicle, origin)].add(trip_id)
    for (vehicle, origin), trips in sorted(seen.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        out.append(
            Finding(
                "vehicle_on_two_trips",
                "vehicle",
                str(vehicle),
                origin,
                origin,
                float(len(trips)),
                f"the same message is reported on trips {', '.join(sorted(trips))}",
            )
        )

    # Real observations only: a duplicate row carries another trip's timestamp and is
    # reported above.
    spans = (
        df.filter(pl.col("vehicle_id").is_not_null() & ~pl.col("handover_duplicate"))
        .group_by("vehicle_id", "trip_id", "trip_start_ts")
        .agg(pl.col("origin_ts").min().alias("first"), pl.col("origin_ts").max().alias("last"))
        .sort("vehicle_id", "first", "last", "trip_id")
    )
    earlier: list[tuple[datetime, str]] = []  # (last, trip) of this vehicle's previous trips
    previous_vehicle: str | None = None
    for vehicle, trip_id, _, first, last in spans.iter_rows():
        if vehicle != previous_vehicle:
            previous_vehicle, earlier = vehicle, []
        # Overlap of two time spans: the earlier end minus the later start.
        overlaps = [
            ((min(e_last, last) - first).total_seconds(), e_trip) for e_last, e_trip in earlier
        ]
        worst = max(overlaps, default=(0.0, ""))
        if worst[0] > trips_th.overlap_s:
            out.append(
                Finding(
                    "vehicle_on_two_trips",
                    "vehicle",
                    str(vehicle),
                    first,
                    min(last, first + timedelta(seconds=worst[0])),
                    worst[0],
                    f"trips {worst[1]} and {trip_id} overlap by {worst[0]:.0f} s",
                )
            )
        earlier.append((last, trip_id))
    return out
