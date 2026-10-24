"""True vehicle movements (ground truth) and the position reports the API would publish.

All times are UTC epoch seconds (floats). Scheduled times come from the GTFS seconds of a
trip plus the service day's "noon minus 12 h" reference, the same rule as
:func:`headway.gtfs.timeconv.gtfs_time_to_utc`.

The true run of a segment is its physical running time (length / scheduled speed) plus a
random increment: larger in peak hours and on bottleneck segments. Because the schedule is
the physical time rounded up to whole minutes (plus padding on padded segments), late
vehicles recover on padded segments. Vehicles hold at timing points when early, dwell at
every stop, and a block's next trip cannot start before the previous one has arrived plus a
minimum turnaround.
"""

import bisect
import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import numpy.typing as npt

from headway.gtfs.timeconv import service_day_reference
from headway.synth.config import DelayConfig, ReportingConfig, TimetableConfig
from headway.synth.network import rng_for
from headway.synth.timetable import SegmentInfo, TripPlan

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]


@dataclass
class TripRun:
    """One trip on one service date: schedule and true movement."""

    plan: TripPlan
    service_date: date
    sched_arr: FloatArray
    sched_dep: FloatArray
    true_arr: FloatArray
    true_dep: FloatArray
    is_peak: BoolArray  # per segment, for the delay model and the truth table
    bp_t: FloatArray = field(init=False)  # (time, distance) breakpoints of the true path
    bp_d: FloatArray = field(init=False)
    _t: list[float] = field(init=False, repr=False)
    _d: list[float] = field(init=False, repr=False)
    _arr: list[float] = field(init=False, repr=False)
    stop_json: list[dict[str, object]] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the piecewise-linear true trajectory: dwell plateaus at stops."""
        stop_d = self.plan.direction.stop_dist
        t = [self.true_arr[0] - 60.0, self.true_dep[0]]
        d = [stop_d[0], stop_d[0]]
        for i in range(1, len(stop_d)):
            t += [self.true_arr[i], self.true_dep[i]]
            d += [stop_d[i], stop_d[i]]
        self.bp_t = np.array(t)
        self.bp_d = np.array(d)
        self._t, self._d = self.bp_t.tolist(), self.bp_d.tolist()
        self._arr = self.true_arr.tolist()

    @property
    def trip_id(self) -> str:
        """GTFS trip id."""
        return self.plan.trip_id

    def distance_at(self, t: float) -> float:
        """True distance along the route at time ``t``."""
        if t <= self._t[0]:
            return self._d[0]
        if t >= self._t[-1]:
            return self._d[-1]
        j = bisect.bisect_right(self._t, t) - 1
        span = self._t[j + 1] - self._t[j]
        f = (t - self._t[j]) / span if span > 0 else 0.0
        return self._d[j] + f * (self._d[j + 1] - self._d[j])

    def stop_index_at(self, t: float) -> int:
        """Index of the last stop reached by time ``t`` (0 before the first departure)."""
        return max(0, bisect.bisect_right(self._arr, t) - 1)

    def dwelling_at(self, t: float) -> int | None:
        """Index of the stop the vehicle is standing at, if any."""
        i = self.stop_index_at(t)
        start = self.true_arr[i] - (60.0 if i == 0 else 0.0)
        return i if start <= t <= self.true_dep[i] else None

    def true_delay_at(self, t: float) -> float:
        """Delay at time ``t``: actual time minus scheduled time at the current position."""
        i = self.stop_index_at(t)
        stop = self.dwelling_at(t)
        if stop is not None:
            arrival = float(self.true_arr[stop] - self.sched_arr[stop]) if stop > 0 else -math.inf
            return max(arrival, t - float(self.sched_dep[stop]))
        if i >= len(self.sched_arr) - 1:
            return t - float(self.sched_arr[-1])
        stop_d = self.plan.direction.stop_dist
        span = stop_d[i + 1] - stop_d[i]
        frac = (self.distance_at(t) - stop_d[i]) / span if span > 0 else 0.0
        sched = float(self.sched_dep[i] + frac * (self.sched_arr[i + 1] - self.sched_dep[i]))
        return t - sched


def simulate_block(
    seed: int,
    service_date: date,
    plans: list[TripPlan],
    segments: dict[int, SegmentInfo],
    speed_kmh: float,
    timetable: TimetableConfig,
    delay: DelayConfig,
    tz: str,
) -> list[TripRun]:
    """Simulate the trips of one vehicle block on one date, in block order."""
    ref = service_day_reference(service_date, tz).timestamp()
    speed = speed_kmh / 3.6
    runs: list[TripRun] = []
    previous_arrival = -math.inf
    for plan in sorted(plans, key=lambda p: p.block_seq):
        rng = rng_for(seed, "trip", service_date.toordinal(), plan.trip_id)
        sched_arr = ref + plan.arr_s.astype(np.float64)
        sched_dep = ref + plan.dep_s.astype(np.float64)
        n = len(sched_arr)
        seg = segments[plan.direction.direction_id]
        lengths = np.diff(plan.direction.stop_dist)
        local_hours = ((plan.dep_s[:-1] // 3600) % 24).astype(int)
        is_peak = np.array(
            [
                plan.service_id == "WD" and any(a <= h < b for a, b in timetable.peak_hours)
                for h in local_hours
            ]
        )
        true_arr = np.empty(n)
        true_dep = np.empty(n)
        initial = rng.normal(delay.initial_delay_s.mean, delay.initial_delay_s.sd)
        true_dep[0] = max(
            sched_dep[0] + max(initial, -30.0), previous_arrival + delay.min_turnaround_s
        )
        true_arr[0] = true_dep[0]
        for i in range(n - 1):
            mean = delay.segment_increment_s.mean * (
                delay.peak_increment_factor if is_peak[i] else 1.0
            )
            mean += delay.bottleneck_increment_s if seg.bottleneck[i] else 0.0
            physical = float(lengths[i]) / speed
            run = max(0.5 * physical, physical + rng.normal(mean, delay.segment_increment_s.sd))
            true_arr[i + 1] = true_dep[i] + run
            if i + 1 == n - 1:
                true_dep[i + 1] = true_arr[i + 1]
                continue
            dwell = max(0.0, rng.normal(delay.dwell_s.mean, delay.dwell_s.sd))
            true_dep[i + 1] = true_arr[i + 1] + dwell
            if sched_dep[i + 1] > sched_arr[i + 1]:  # timing point: never leave early
                true_dep[i + 1] = max(true_dep[i + 1], sched_dep[i + 1])
        previous_arrival = float(true_arr[-1])
        runs.append(TripRun(plan, service_date, sched_arr, sched_dep, true_arr, true_dep, is_peak))
    return runs


@dataclass
class Reports:
    """The vehicle messages of one trip and when the API publishes each of them."""

    t: FloatArray  # message (origin) times
    published: FloatArray  # non-decreasing publication times
    noise_xy: FloatArray  # (n, 2) GPS error in metres, including planted defects
    golemio_noise: FloatArray  # error of the API's own delay estimate, seconds
    off_shape: BoolArray  # planted off-shape offset applied

    def latest_before(self, when: float) -> int | None:
        """Index of the newest message published by ``when``."""
        k = int(np.searchsorted(self.published, when, side="right")) - 1
        return k if k >= 0 else None


@dataclass(frozen=True)
class TripDefects:
    """Planted problems affecting one trip's reports (ground truth)."""

    gps_jump_at: float | None = None
    gps_jump_m: float = 0.0
    off_shape_from: float | None = None
    off_shape_to: float | None = None
    off_shape_m: float = 0.0
    silent_from: float | None = None
    silent_to: float | None = None


def make_reports(seed: int, run: TripRun, cfg: ReportingConfig, defects: TripDefects) -> Reports:
    """Generate the message stream of one trip."""
    rng = rng_for(seed, "reports", run.service_date.toordinal(), run.trip_id)
    start, end = float(run.bp_t[0]), float(run.true_arr[-1])
    times = [start]
    while times[-1] < end:
        times.append(min(end, times[-1] + rng.uniform(*cfg.report_interval_s)))
    if len(times) > 2 and times[-1] - times[-2] < cfg.report_interval_s[0]:
        # The last message is clamped to the arrival. If it crowds the one before it, both
        # would print the same second as origin_timestamp but carry different content.
        del times[-2]
    t = np.array(times)
    jumped_at: float | None = None
    if defects.gps_jump_at is not None:
        jumped_at = float(t[int(np.argmin(np.abs(t - defects.gps_jump_at)))])
    if defects.silent_from is not None and defects.silent_to is not None:
        t = t[(t < defects.silent_from) | (t > defects.silent_to)]
    n = len(t)
    lag = rng.exponential(cfg.publish_lag_s, size=n)
    long = rng.random(n) < cfg.long_lag_probability
    lag[long] = rng.uniform(*cfg.long_lag_s, size=int(long.sum()))
    published = np.maximum.accumulate(t + lag)
    noise = rng.normal(0.0, cfg.gps_noise_m, size=(n, 2))
    off = np.zeros(n, dtype=bool)
    path = run.plan.direction.path
    if defects.off_shape_from is not None and defects.off_shape_to is not None:
        off = (t >= defects.off_shape_from) & (t <= defects.off_shape_to)
        for k in np.flatnonzero(off):
            ux, uy = path.heading_at(run.distance_at(float(t[k])))
            noise[k] += (uy * defects.off_shape_m, -ux * defects.off_shape_m)  # to the right
    if jumped_at is not None:
        # The report nearest the planted time, chosen before silent periods removed any. If
        # that report is gone, the jump never happens (and is invisible).
        angle = rng.uniform(0, 2 * math.pi)
        for j in np.flatnonzero(t == jumped_at):
            noise[j] += (defects.gps_jump_m * math.cos(angle), defects.gps_jump_m * math.sin(angle))
    golemio = rng.normal(0.0, cfg.golemio_delay_noise_s, size=n)
    return Reports(t, published, noise, golemio, off)
