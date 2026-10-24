"""Synthetic timetable: trips, scheduled stop times and vehicle blocks.

Scheduled times are integer GTFS seconds (from "noon minus 12 h" of the service day),
rounded to whole minutes like real timetables. Every ``timing_point_every``-th stop has a
one-minute scheduled dwell; elsewhere arrival equals departure. Some segments get extra
running time ("padded"), which lets late vehicles recover; others are bottlenecks in the
delay model. Both are part of the ground truth.
"""

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from headway.gtfs.timeconv import parse_gtfs_time
from headway.synth.config import TimetableConfig
from headway.synth.network import Direction, Route, rng_for

IntArray = npt.NDArray[np.int64]
SERVICES = ("WD", "WE")  # Monday-Friday, Saturday-Sunday


@dataclass(frozen=True)
class SegmentInfo:
    """Planned properties of the segments of one route direction (ground truth)."""

    padded: tuple[bool, ...]
    bottleneck: tuple[bool, ...]


@dataclass(frozen=True)
class TripPlan:
    """One scheduled trip (repeats on every day of its service)."""

    trip_id: str
    route: Route
    direction: Direction
    service_id: str
    arr_s: IntArray  # scheduled arrival per stop, GTFS seconds
    dep_s: IntArray  # scheduled departure per stop, GTFS seconds
    block_id: str
    block_seq: int  # 1-based position of the trip in its block
    vehicle_reg: int


def _headway(cfg: TimetableConfig, service: str, gtfs_s: int) -> int:
    hour = (gtfs_s // 3600) % 24
    if cfg.night_hours[0] <= hour < cfg.night_hours[1]:
        minutes = cfg.headway_min["night"]
    elif service == "WE":
        minutes = cfg.headway_min["weekend"]
    elif any(a <= hour < b for a, b in cfg.peak_hours):
        minutes = cfg.headway_min["peak"]
    elif hour >= cfg.evening_from_hour:
        minutes = cfg.headway_min["evening"]
    else:
        minutes = cfg.headway_min["day"]
    return minutes * 60


def segment_info(
    seed: int, route: Route, n_padded: int, n_bottlenecks: int
) -> dict[int, SegmentInfo]:
    """Choose padded and bottleneck segments for both directions of a route."""
    info = {}
    for direction in route.directions:
        n_seg = len(direction.stops) - 1
        rng = rng_for(seed, "segments", route.route_id, direction.direction_id)
        order = rng.permutation(n_seg)
        padded = set(order[:n_padded].tolist())
        bottleneck = set(order[n_padded : n_padded + n_bottlenecks].tolist())
        info[direction.direction_id] = SegmentInfo(
            tuple(i in padded for i in range(n_seg)), tuple(i in bottleneck for i in range(n_seg))
        )
    return info


def _stop_times(
    direction: Direction, speed_kmh: float, seg: SegmentInfo, cfg: TimetableConfig, dep0: int
) -> tuple[IntArray, IntArray]:
    speed = speed_kmh / 3.6
    arr, dep = [dep0], [dep0]
    for i, length in enumerate(np.diff(direction.stop_dist)):
        exact = dep[-1] + float(length) / speed + (60.0 if seg.padded[i] else 0.0)
        a = int(math.ceil(exact / 60.0) * 60)
        is_timing_point = (i + 1) % cfg.timing_point_every == 0 and i + 1 < len(direction.stops) - 1
        arr.append(a)
        dep.append(a + 60 if is_timing_point else a)
    return np.array(arr, dtype=np.int64), np.array(dep, dtype=np.int64)


def build_timetable(
    seed: int,
    routes: list[Route],
    segments: dict[str, dict[int, SegmentInfo]],
    cfg: TimetableConfig,
    *,
    version: int = 1,
) -> list[TripPlan]:
    """Build every trip of every service. Version 2 shifts route L1 by the configured minutes."""
    first, last = parse_gtfs_time(cfg.first_departure), parse_gtfs_time(cfg.last_departure)
    trips: list[TripPlan] = []
    ordinal: dict[str, int] = {}
    for route_index, route in enumerate(routes, start=1):
        # Trams and regional buses use the same fleet-number range, so numbers collide
        # across them, as observed in the real feed (docs/api.md); city buses use another.
        ordinal[route.kind] = ordinal.get(route.kind, -1) + 1
        reg_base = (9100 if route.kind in ("tram", "regional_bus") else 7100) + 30 * ordinal[
            route.kind
        ]
        speed = cfg.speed_kmh["tram" if route.route_type == 0 else "bus"]
        shift = cfg.timetable_change_shift_min * 60 if version == 2 and route_index == 1 else 0
        suffix = "_v2" if version == 2 and route_index == 1 else ""
        for service in SERVICES:
            planned: list[tuple[int, Direction, IntArray, IntArray]] = []
            for direction in route.directions:
                t = first + shift + direction.direction_id * 120
                while t <= last + shift:
                    arr, dep = _stop_times(
                        direction, speed, segments[route.route_id][direction.direction_id], cfg, t
                    )
                    planned.append((t, direction, arr, dep))
                    t += _headway(cfg, service, t)
            trips += _assign_blocks(route, reg_base, service, planned, cfg, suffix)
    return trips


def _assign_blocks(
    route: Route,
    reg_base: int,
    service: str,
    planned: list[tuple[int, Direction, IntArray, IntArray]],
    cfg: TimetableConfig,
    suffix: str,
) -> list[TripPlan]:
    # Greedy vehicle scheduling: a vehicle that finished a trip may take the next departure
    # from the terminal where it arrived, after the minimum layover.
    vehicles: list[dict[str, int]] = []  # {"free_at", "next_dir", "trips"}
    out: list[TripPlan] = []
    for dep0, direction, arr, dep in sorted(planned, key=lambda p: (p[0], p[1].direction_id)):
        ready = [
            k
            for k, v in enumerate(vehicles)
            if v["next_dir"] == direction.direction_id and v["free_at"] <= dep0
        ]
        k = min(ready, key=lambda j: vehicles[j]["free_at"]) if ready else len(vehicles)
        if k == len(vehicles):
            vehicles.append({"free_at": 0, "next_dir": direction.direction_id, "trips": 0})
        vehicle = vehicles[k]
        vehicle["trips"] += 1
        vehicle["free_at"] = int(arr[-1]) + cfg.layover_min * 60
        vehicle["next_dir"] = 1 - direction.direction_id
        hhmm = f"{dep0 // 3600:02d}{dep0 // 60 % 60:02d}"
        out.append(
            TripPlan(
                trip_id=f"{route.route_id}_{direction.direction_id}_{service}_{hhmm}{suffix}",
                route=route,
                direction=direction,
                service_id=service,
                arr_s=arr,
                dep_s=dep,
                block_id=f"{route.route_id}_{service}_B{k + 1:02d}",
                block_seq=vehicle["trips"],
                vehicle_reg=reg_base + k + 1,
            )
        )
    return out
