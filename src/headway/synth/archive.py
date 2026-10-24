"""Generate a synthetic raw archive in exactly the collector's format, with ground truth.

Output layout (``out``)::

    SYNTHETIC.json                      marker: parameters, period, counts
    raw/YYYY/MM/DD/HHMMSS_<sha12>.json.gz   bodies, written with the collector's functions
    raw/manifest/YYYY-MM-DD.parquet     one row per HTTP attempt (collector schema)
    gtfs/PID_GTFS_<date>_<sha12>.zip    timetable versions + gtfs_versions.parquet
    truth/stop_events.parquet           true arrival/departure of every trip at every stop
    truth/segments.parquet              planted segment effects (bottleneck, padding)
    truth/defects.parquet               every planted defect with time and entity

Nothing outside ``truth/`` and ``SYNTHETIC.json`` reveals that the data is synthetic,
except the names (``SYNTH ...`` operators, ``SYN..`` stop ids), so the pipeline is
exercised exactly as on real data while the truth stays available for evaluation.
"""

import hashlib
import itertools
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from headway.collect.manifest import ManifestWriter, PollRecord
from headway.collect.storage import compress, raw_relpath
from headway.fileio import write_atomic
from headway.gtfs.timeconv import DEFAULT_TZ, parse_gtfs_time, service_day_reference
from headway.synth.config import SynthConfig
from headway.synth.gtfs_writer import gtfs_files, period_end, write_gtfs_snapshot
from headway.synth.network import Plane, build_network, rng_for
from headway.synth.render import Renderer
from headway.synth.simulate import Reports, TripDefects, TripRun, make_reports, simulate_block
from headway.synth.timetable import SegmentInfo, TripPlan, build_timetable, segment_info

GENERATOR_VERSION = "synth-1"
ERROR_BODY = b'{"error_message":"Service Unavailable","error_status":503}'


@dataclass
class SynthResult:
    """Summary of a generation run."""

    out_dir: Path
    polls: int = 0
    files: int = 0
    stored_bytes: int = 0
    raw_bytes: int = 0
    trips: int = 0
    defects: int = 0
    first_poll: datetime | None = None
    last_poll: datetime | None = None


@dataclass
class _Active:
    run: TripRun
    reports: Reports
    shown: bool = False


@dataclass
class _World:
    cfg: SynthConfig
    tz: str
    plane: Plane = field(init=False)
    routes: list[Any] = field(init=False)
    segments: dict[str, dict[int, SegmentInfo]] = field(init=False)
    versions: list[tuple[date, list[TripPlan]]] = field(init=False)
    bias_route: str = field(init=False)

    def __post_init__(self) -> None:
        cfg = self.cfg
        self.plane = Plane(cfg.network.origin_lat, cfg.network.origin_lon)
        self.routes = build_network(cfg.seed, cfg.network)
        self.segments = {
            r.route_id: segment_info(
                cfg.seed,
                r,
                cfg.timetable.padded_segments_per_route,
                cfg.delay.bottlenecks_per_route,
            )
            for r in self.routes
        }
        self.versions = [
            (cfg.start_date, build_timetable(cfg.seed, self.routes, self.segments, cfg.timetable))
        ]
        change = cfg.timetable.timetable_change_day
        if 0 < change < cfg.days:
            v2 = build_timetable(cfg.seed, self.routes, self.segments, cfg.timetable, version=2)
            self.versions.append((cfg.start_date + timedelta(days=change), v2))
        self.bias_route = self.routes[-1].route_id  # the last route gets the delay bias

    def plans_for(self, day: date) -> list[TripPlan]:
        valid = [plans for since, plans in self.versions if since <= day][-1]
        service = "WD" if day.weekday() < 5 else "WE"
        return [p for p in valid if p.service_id == service]

    def simulate_day(self, day: date) -> list[TripRun]:
        blocks: dict[str, list[TripPlan]] = {}
        for plan in self.plans_for(day):
            blocks.setdefault(plan.block_id, []).append(plan)
        runs: list[TripRun] = []
        for block_id in sorted(blocks):
            plans = blocks[block_id]
            route = plans[0].route
            speed = self.cfg.timetable.speed_kmh["tram" if route.route_type == 0 else "bus"]
            runs += simulate_block(
                self.cfg.seed,
                day,
                plans,
                self.segments[route.route_id],
                speed,
                self.cfg.timetable,
                self.cfg.delay,
                self.tz,
            )
        return runs


def _poisson_windows(
    rng: Any, rate: float, lo: float, hi: float, length: tuple[float, float]
) -> list[tuple[float, float]]:
    windows = []
    for _ in range(int(rng.poisson(rate))):
        start = rng.uniform(lo, hi)
        windows.append((start, start + rng.uniform(*length)))
    return windows


def _trip_defects(
    world: _World, day: date, runs: list[TripRun]
) -> tuple[dict[str, TripDefects], list[dict[str, Any]]]:
    d = world.cfg.defects
    rng = rng_for(world.cfg.seed, "trip-defects", day.toordinal())
    found: dict[str, TripDefects] = {}
    truth: list[dict[str, Any]] = []
    if not runs:
        return found, truth

    used: set[tuple[str, str]] = set()

    def pick(kind: str) -> tuple[TripRun, float] | None:
        # A trip carries at most one defect of each kind: a second one would replace the
        # first in the data while staying in the truth table.
        run = runs[int(rng.integers(len(runs)))]
        t = float(rng.uniform(run.true_dep[0], run.true_arr[-1]))
        if (kind, run.trip_id) in used:
            return None
        used.add((kind, run.trip_id))
        return run, t

    for _ in range(int(rng.poisson(d.gps_jumps))):
        picked = pick("gps_jump")
        if picked is None:
            continue
        run, t = picked
        m = float(rng.uniform(*d.gps_jump_m))
        found[run.trip_id] = replace(
            found.get(run.trip_id, TripDefects()), gps_jump_at=t, gps_jump_m=m
        )
        truth.append(_defect("gps_jump", t, t, run, f"{m:.0f} m"))
    for _ in range(int(rng.poisson(d.off_shape))):
        picked = pick("off_shape")
        if picked is None:
            continue
        run, t = picked
        m = float(rng.uniform(*d.off_shape_m))
        end = t + 60 * float(rng.uniform(*d.off_shape_min))
        found[run.trip_id] = replace(
            found.get(run.trip_id, TripDefects()), off_shape_from=t, off_shape_to=end, off_shape_m=m
        )
        truth.append(_defect("off_shape", t, end, run, f"{m:.0f} m"))
    for _ in range(int(rng.poisson(d.silent_trips))):
        picked = pick("silent_trip")
        if picked is None:
            continue
        run, t = picked
        end = t + 60 * float(rng.uniform(*d.silent_min))
        found[run.trip_id] = replace(
            found.get(run.trip_id, TripDefects()), silent_from=t, silent_to=end
        )
        truth.append(_defect("silent_trip", t, end, run, f"{(end - t) / 60:.1f} min"))
    return found, truth


def _overlaps_window(
    defect: dict[str, Any], window_from: float, window_to: float, margin_s: float
) -> bool:
    """True if the defect is active at some time in the polled window.

    A defect that ends just before the first poll can still be in the data: the first poll
    shows messages up to ``margin_s`` old.
    """
    return bool(
        defect["start"].timestamp() < window_to
        and defect["end"].timestamp() >= window_from - margin_s
    )


def _defect(
    kind: str, start: float, end: float, run: TripRun | None, details: str
) -> dict[str, Any]:
    return {
        "kind": kind,
        "start": datetime.fromtimestamp(start, UTC),
        "end": datetime.fromtimestamp(end, UTC),
        "trip_id": run.trip_id if run else None,
        "service_date": run.service_date if run else None,
        "vehicle_registration_number": run.plan.vehicle_reg if run else None,
        "route_type": run.plan.route.route_type if run else None,
        "details": details,
    }


@dataclass
class _CollectorPlan:
    gaps: list[tuple[float, float]]
    errors: set[float]
    stale_starts: dict[float, int]


def _collector_plan(
    world: _World, day: date, ticks: list[float]
) -> tuple[_CollectorPlan, list[dict[str, Any]]]:
    d = world.cfg.defects
    rng = rng_for(world.cfg.seed, "collector", day.toordinal())
    lo, hi = ticks[0], ticks[-1]
    # _poisson_windows returns (start, start + length in minutes); convert to seconds.
    windows = _poisson_windows(rng, d.collection_gaps, lo, hi, d.collection_gap_min)
    gaps = [(a, a + (b - a) * 60) for a, b in windows]

    def in_gap(t: float) -> bool:
        return any(a <= t <= b for a, b in gaps)

    errors = {ticks[int(rng.integers(len(ticks)))] for _ in range(int(rng.poisson(d.api_errors)))}
    stale = {
        ticks[int(rng.integers(len(ticks)))]: int(
            rng.integers(d.stale_polls[0], d.stale_polls[1] + 1)
        )
        for _ in range(int(rng.poisson(d.stale_episodes)))
    }
    # Planted events that would fall inside a gap never happen; keep the truth exact.
    errors = {t for t in errors if not in_gap(t)}
    stale = {t: n for t, n in stale.items() if not in_gap(t) and t not in errors}
    truth = [_defect("collection_gap", a, b, None, f"{(b - a) / 60:.1f} min") for a, b in gaps]
    truth += [_defect("api_error", t, t, None, "HTTP 503 x3") for t in sorted(errors)]
    step = world.cfg.poll_interval_s
    truth += [
        _defect("stale_feed", t, t + (n - 1) * step, None, f"{n} identical polls")
        for t, n in sorted(stale.items())
    ]
    return _CollectorPlan(gaps, errors, stale), truth


def _ticks(start: float, end: float, step: int) -> Iterator[float]:
    t = math.ceil(start / step) * step
    while t < end:
        yield float(t)
        t += step


STOP_EVENT_SCHEMA = pa.schema(
    [
        ("service_date", pa.date32()),
        ("trip_id", pa.string()),
        ("route_id", pa.string()),
        ("direction_id", pa.int8()),
        ("block_id", pa.string()),
        ("vehicle_registration_number", pa.int32()),
        ("stop_sequence", pa.int32()),
        ("stop_id", pa.string()),
        ("sched_arr", pa.timestamp("ms", tz="UTC")),
        ("sched_dep", pa.timestamp("ms", tz="UTC")),
        ("true_arr", pa.timestamp("ms", tz="UTC")),
        ("true_dep", pa.timestamp("ms", tz="UTC")),
        ("arr_delay_s", pa.float64()),
        ("dep_delay_s", pa.float64()),
    ]
)
DEFECT_SCHEMA = pa.schema(
    [
        ("kind", pa.string()),
        ("start", pa.timestamp("us", tz="UTC")),
        ("end", pa.timestamp("us", tz="UTC")),
        ("trip_id", pa.string()),
        ("service_date", pa.date32()),
        ("vehicle_registration_number", pa.int32()),
        ("route_type", pa.int8()),
        ("details", pa.string()),
    ]
)
SEGMENT_SCHEMA = pa.schema(
    [
        ("route_id", pa.string()),
        ("direction_id", pa.int8()),
        ("from_stop_sequence", pa.int32()),
        ("to_stop_sequence", pa.int32()),
        ("from_stop_id", pa.string()),
        ("to_stop_id", pa.string()),
        ("length_m", pa.float64()),
        ("bottleneck", pa.bool_()),
        ("padded", pa.bool_()),
        ("mean_increment_offpeak_s", pa.float64()),
        ("mean_increment_peak_s", pa.float64()),
    ]
)


def _stop_event_rows(runs: list[TripRun]) -> Iterator[dict[str, Any]]:
    def ts(v: float) -> datetime:
        return datetime.fromtimestamp(v, UTC)

    for run in runs:
        for i, stop in enumerate(run.plan.direction.stops):
            yield {
                "service_date": run.service_date,
                "trip_id": run.trip_id,
                "route_id": run.plan.route.route_id,
                "direction_id": run.plan.direction.direction_id,
                "block_id": run.plan.block_id,
                "vehicle_registration_number": run.plan.vehicle_reg,
                "stop_sequence": i + 1,
                "stop_id": stop.stop_id,
                "sched_arr": ts(float(run.sched_arr[i])),
                "sched_dep": ts(float(run.sched_dep[i])),
                "true_arr": ts(float(run.true_arr[i])),
                "true_dep": ts(float(run.true_dep[i])),
                "arr_delay_s": float(run.true_arr[i] - run.sched_arr[i]),
                "dep_delay_s": float(run.true_dep[i] - run.sched_dep[i]),
            }


def _segment_rows(world: _World) -> list[dict[str, Any]]:
    delay = world.cfg.delay
    rows = []
    for route in world.routes:
        for direction in route.directions:
            seg = world.segments[route.route_id][direction.direction_id]
            for i in range(len(direction.stops) - 1):
                base = delay.bottleneck_increment_s if seg.bottleneck[i] else 0.0
                rows.append(
                    {
                        "route_id": route.route_id,
                        "direction_id": direction.direction_id,
                        "from_stop_sequence": i + 1,
                        "to_stop_sequence": i + 2,
                        "from_stop_id": direction.stops[i].stop_id,
                        "to_stop_id": direction.stops[i + 1].stop_id,
                        "length_m": float(direction.stop_dist[i + 1] - direction.stop_dist[i]),
                        "bottleneck": seg.bottleneck[i],
                        "padded": seg.padded[i],
                        "mean_increment_offpeak_s": delay.segment_increment_s.mean + base,
                        "mean_increment_peak_s": delay.segment_increment_s.mean
                        * delay.peak_increment_factor
                        + base,
                    }
                )
    return rows


def _write_parquet(path: Path, rows: list[dict[str, Any]], schema: pa.Schema) -> None:
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), sink, compression="zstd")
    write_atomic(path, sink.getvalue().to_pybytes())


def generate(
    cfg: SynthConfig,
    out: Path,
    *,
    start_at: datetime | None = None,
    hours: float | None = None,
    tz: str = DEFAULT_TZ,
) -> SynthResult:
    """Generate the archive. ``start_at``/``hours`` limit the polled window (for tests)."""
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty; choose another output directory")
    raw_dir, gtfs_dir, truth_dir = out / "raw", out / "gtfs", out / "truth"
    world = _World(cfg, tz)
    last_day = period_end(cfg.start_date, cfg.days)

    # Timetable versions, stored and registered exactly like downloaded snapshots.
    for since, plans in world.versions:
        write_gtfs_snapshot(
            gtfs_dir, since, gtfs_files(world.routes, plans, world.plane, since, last_day)
        )

    # The polled period follows the service: from just before the first departure of the
    # first service date until the night trips of the last service date have finished.
    first_s = parse_gtfs_time(cfg.timetable.first_departure)
    period_from = service_day_reference(cfg.start_date, tz).timestamp() + first_s - 60
    last_arrival = max(float(r.true_arr[-1]) for r in world.simulate_day(last_day))
    period_to = last_arrival + cfg.reporting.linger_s
    window_from = max(period_from, start_at.timestamp()) if start_at else period_from
    window_to = min(period_to, window_from + hours * 3600) if hours else period_to

    renderer = Renderer(world.plane, cfg.reporting, world.bias_route, tz)
    manifest = ManifestWriter(raw_dir / "manifest")
    result = SynthResult(out)
    all_runs: list[TripRun] = []
    defects: list[dict[str, Any]] = [
        _defect(
            "delay_bias",
            period_from,
            period_to,
            None,
            f"route {world.bias_route}: +{cfg.reporting.golemio_delay_bias_s:.0f} s",
        )
    ]
    handovers: dict[str, str] = {}  # next trip id -> previous trip id, for duplicates
    predecessors: set[str] = set()  # previous trips of planned handovers
    retired: dict[str, _Active] = {}  # predecessors that have left the feed
    pending: list[tuple[float, TripRun, TripDefects]] = []
    simulated: set[date] = set()
    active: list[_Active] = []
    previous_body: bytes | None = None
    stale_left = 0
    collector: dict[date, _CollectorPlan] = {}
    current_day: date | None = None

    def ensure_days(until: float) -> None:
        day = cfg.start_date
        while day <= last_day:
            if day not in simulated and service_day_reference(day, tz).timestamp() - 3600 <= until:
                simulated.add(day)
                runs = world.simulate_day(day)
                all_runs.extend(runs)
                trip_defects, truth = _trip_defects(world, day, runs)
                defects.extend(truth)
                by_block: dict[str, list[TripRun]] = {}
                for run in runs:
                    by_block.setdefault(run.plan.block_id, []).append(run)
                    pending.append(
                        (float(run.bp_t[0]), run, trip_defects.get(run.trip_id, TripDefects()))
                    )
                rng = rng_for(cfg.seed, "handover", day.toordinal())
                for block in by_block.values():
                    block.sort(key=lambda r: r.plan.block_seq)
                    for a, b in itertools.pairwise(block):
                        if rng.random() < cfg.defects.handover_duplicate_probability:
                            handovers[b.trip_id] = a.trip_id
                            predecessors.add(a.trip_id)
                pending.sort(key=lambda p: p[0], reverse=True)
            day += timedelta(days=1)

    for tick in _ticks(window_from, window_to, cfg.poll_interval_s):
        ensure_days(tick + 2 * 3600)
        utc_day = datetime.fromtimestamp(tick, UTC).date()
        if utc_day != current_day:
            if current_day is not None:
                manifest.flush()
            current_day = utc_day
            day_start = datetime(utc_day.year, utc_day.month, utc_day.day, tzinfo=UTC).timestamp()
            day_ticks = list(_ticks(day_start, day_start + 86400, cfg.poll_interval_s))
            plan, truth = _collector_plan(world, utc_day, day_ticks)
            collector[utc_day] = plan
            defects.extend(
                row
                for row in truth
                if _overlaps_window(row, window_from, window_to, cfg.reporting.drop_after_s)
            )
        plan = collector[utc_day]

        # Activate trips whose first message may be published by now.
        while pending and pending[-1][0] <= tick:
            _, run, trip_defect = pending.pop()
            active.append(_Active(run, make_reports(cfg.seed, run, cfg.reporting, trip_defect)))
        staying = []
        for a in active:
            if tick <= a.reports.published[-1] + cfg.reporting.linger_s:
                staying.append(a)
            elif a.run.trip_id in predecessors:
                retired[a.run.trip_id] = a
        active = staying

        if any(a <= tick <= b for a, b in plan.gaps):
            continue  # collector down: no request, no manifest row
        jitter = rng_for(cfg.seed, "tick", int(tick))
        if tick in plan.errors:
            offset = 0.0
            for attempt in (1, 2, 3):
                fetched = tick + offset + float(jitter.uniform(0.002, 0.05))
                _store(
                    raw_dir,
                    manifest,
                    result,
                    fetched,
                    attempt,
                    503,
                    ERROR_BODY,
                    float(jitter.uniform(80, 200)),
                )
                offset += 2.0 ** (attempt - 1)
            continue

        if tick in plan.stale_starts and previous_body is not None:
            stale_left = plan.stale_starts[tick] - 1
            body = previous_body
        elif stale_left > 0 and previous_body is not None:
            stale_left -= 1
            body = previous_body
        else:
            body = _render_poll(
                renderer, active, tick, cfg.reporting.drop_after_s, handovers, retired, defects
            )
        previous_body = body
        fetched = tick + float(jitter.uniform(0.002, 0.05))
        _store(raw_dir, manifest, result, fetched, 1, 200, body, float(jitter.uniform(180, 420)))
        result.polls += 1
        result.first_poll = result.first_poll or datetime.fromtimestamp(fetched, UTC)
        result.last_poll = datetime.fromtimestamp(fetched, UTC)
    manifest.flush()

    in_window = [r for r in all_runs if r.true_arr[-1] >= window_from and r.bp_t[0] <= window_to]
    result.trips = len(in_window)
    defects = [
        d
        for d in defects
        if d["kind"] == "delay_bias"
        or _overlaps_window(d, window_from, window_to, cfg.reporting.drop_after_s)
    ]
    result.defects = len(defects)
    _write_parquet(
        truth_dir / "stop_events.parquet", list(_stop_event_rows(in_window)), STOP_EVENT_SCHEMA
    )
    _write_parquet(truth_dir / "segments.parquet", _segment_rows(world), SEGMENT_SCHEMA)
    _write_parquet(
        truth_dir / "defects.parquet",
        sorted(defects, key=lambda d: (d["start"], d["kind"], d["trip_id"] or "")),
        DEFECT_SCHEMA,
    )
    marker = {
        "synthetic": True,
        "generator": GENERATOR_VERSION,
        "note": "Synthetic data: demonstrates the pipeline; says nothing about real Prague.",
        "window": {
            "from": datetime.fromtimestamp(window_from, UTC).isoformat(),
            "to": datetime.fromtimestamp(window_to, UTC).isoformat(),
        },
        "delay_bias_route": world.bias_route,
        "counts": {
            "polls": result.polls,
            "files": result.files,
            "trips": result.trips,
            "defects": result.defects,
            "stored_bytes": result.stored_bytes,
        },
        "config": json.loads(cfg.model_dump_json()),
    }
    (out / "SYNTHETIC.json").write_text(
        json.dumps(marker, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return result


def _render_poll(
    renderer: Renderer,
    active: list[_Active],
    tick: float,
    drop_after_s: float,
    handovers: dict[str, str],
    retired: dict[str, _Active],
    defects: list[dict[str, Any]],
) -> bytes:
    features: list[dict[str, Any]] = []
    by_trip = {a.run.trip_id: a for a in active}
    for item in sorted(active, key=lambda a: a.run.trip_id):
        k = item.reports.latest_before(tick)
        if k is None or tick - item.reports.t[k] > drop_after_s:
            continue
        feature = renderer.feature(item.run, item.reports, k)
        features.append(feature)
        first_appearance = not item.shown
        item.shown = True
        previous = handovers.get(item.run.trip_id)
        prev = by_trip.get(previous) or retired.pop(previous, None) if previous else None
        if first_appearance and prev is not None:
            kp = len(prev.reports.t) - 1
            dup = renderer.feature(prev.run, prev.reports, kp)
            dup["geometry"] = feature["geometry"]
            dup["properties"]["last_position"]["origin_timestamp"] = feature["properties"][
                "last_position"
            ]["origin_timestamp"]
            dup["properties"]["last_position"]["state_position"] = "on_track"
            features.append(dup)
            defects.append(
                _defect(
                    "handover_duplicate",
                    tick,
                    tick,
                    prev.run,
                    f"also reported as {item.run.trip_id}",
                )
            )
    features.sort(key=lambda f: f["properties"]["trip"]["gtfs"]["trip_id"])
    body = {"features": features, "type": "FeatureCollection"}
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _store(
    raw_dir: Path,
    manifest: ManifestWriter,
    result: SynthResult,
    fetched_ts: float,
    attempt: int,
    status: int,
    body: bytes,
    latency_ms: float,
) -> None:
    fetched = datetime.fromtimestamp(fetched_ts, UTC)
    digest = hashlib.sha256(body).hexdigest()
    rel = raw_relpath(fetched, digest)
    stored = compress(body)
    write_atomic(raw_dir / rel, stored, fsync=False)
    manifest.add(
        PollRecord(
            fetched_at=fetched,
            attempt=attempt,
            http_status=status,
            sha256=digest,
            bytes=len(body),
            stored_bytes=len(stored),
            latency_ms=round(latency_ms, 1),
            path=rel.as_posix(),
            error=None if status == 200 else f"HTTP {status}",
        )
    )
    result.files += 1
    result.stored_bytes += len(stored)
    result.raw_bytes += len(body)
