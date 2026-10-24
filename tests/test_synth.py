"""Tests for the synthetic archive generator: format, determinism, truth and defects."""

import collections
import functools
import gzip
import json
import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from headway.cli import app
from headway.collect import PollRecord, read_manifest
from headway.contracts.golemio_v1 import VehiclePositionsFeed, extra_field_paths, parse_feed
from headway.gtfs import gtfs_time_to_utc, load_feed, read_registry, resolve_gtfs
from headway.synth import SynthConfig, generate, load_synth_config

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "synth.toml"
# Monday 2026-09-28, 07:00-08:00 local (morning peak).
WINDOW = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)


def small_cfg(**updates: Any) -> SynthConfig:
    """A smaller network with frequent defects so that 1-2 hours contain every kind."""
    base = load_synth_config(CONFIG).model_dump()
    base.update(days=2)
    base["network"].update(tram_routes=2, bus_routes=2)
    base["timetable"].update(timetable_change_day=0)
    base["defects"].update(
        collection_gaps=40.0,
        collection_gap_min=[1.0, 2.0],
        api_errors=60.0,
        stale_episodes=40.0,
        gps_jumps=150.0,
        off_shape=150.0,
        silent_trips=150.0,
        handover_duplicate_probability=0.5,
    )
    for key, value in updates.items():
        if isinstance(value, dict):
            base[key].update(value)
        else:
            base[key] = value
    return SynthConfig.model_validate(base)


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("synth") / "archive"
    generate(small_cfg(), out, start_at=WINDOW, hours=1.0)
    return out


def manifest_rows(out: Path) -> list[PollRecord]:
    return [
        r for f in sorted((out / "raw" / "manifest").glob("*.parquet")) for r in read_manifest(f)
    ]


@functools.cache
def feeds(out: Path) -> list[tuple[PollRecord, VehiclePositionsFeed]]:
    return [
        (r, parse_feed(gzip.decompress((out / "raw" / str(r.path)).read_bytes())))
        for r in manifest_rows(out)
        if r.http_status == 200
    ]


def truth(out: Path, name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(out / "truth" / f"{name}.parquet").to_pylist()
    return rows


def test_layout_and_marker(archive: Path) -> None:
    marker = json.loads((archive / "SYNTHETIC.json").read_text(encoding="utf-8"))
    assert marker["synthetic"] is True
    assert marker["counts"]["polls"] > 150
    for name in ("stop_events", "segments", "defects"):
        assert (archive / "truth" / f"{name}.parquet").exists()
    assert list((archive / "gtfs").glob("PID_GTFS_*.zip"))
    assert all(p.name.endswith(".json.gz") for p in (archive / "raw" / "2026").rglob("*.*"))


def test_every_body_matches_the_real_contract(archive: Path) -> None:
    polls = feeds(archive)
    assert len(polls) > 150
    for _, feed in polls:
        assert extra_field_paths(feed) == set()
    assert max(len(feed.features) for _, feed in polls) > 20


def test_generation_is_deterministic(archive: Path, tmp_path: Path) -> None:
    again = tmp_path / "again"
    generate(small_cfg(), again, start_at=WINDOW, hours=1.0)
    assert [(r.fetched_at, r.sha256) for r in manifest_rows(again)] == [
        (r.fetched_at, r.sha256) for r in manifest_rows(archive)
    ]
    for name in ("stop_events", "segments", "defects"):
        assert truth(again, name) == truth(archive, name)


def test_refuses_a_non_empty_output_directory(archive: Path) -> None:
    with pytest.raises(FileExistsError):
        generate(small_cfg(), archive, start_at=WINDOW, hours=0.1)


def test_scheduled_times_match_the_resolved_gtfs(archive: Path) -> None:
    day = date(2026, 9, 28)
    feed = load_feed(resolve_gtfs(day, archive / "gtfs"), tables=("stop_times",))
    schedule = {
        (row["trip_id"], row["stop_sequence"]): row["departure_time"]
        for row in feed["stop_times"].iter_rows(named=True)
    }
    checked = 0
    for _, poll in feeds(archive)[:40]:
        for f in poll.features:
            stop = f.properties.last_position.last_stop
            key = (f.properties.trip.gtfs.trip_id, stop.sequence)
            assert stop.departure_time == gtfs_time_to_utc(day, schedule[key])
            checked += 1
    assert checked > 500


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    k = 111_320.0
    return math.hypot((lat2 - lat1) * k, (lon2 - lon1) * k * math.cos(math.radians(lat1)))


def _defect_trips(out: Path, kinds: set[str]) -> set[str]:
    return {d["trip_id"] for d in truth(out, "defects") if d["kind"] in kinds and d["trip_id"]}


def test_vehicles_at_a_stop_are_reported_at_that_stop(archive: Path) -> None:
    stops = load_feed(resolve_gtfs(date(2026, 9, 28), archive / "gtfs"), tables=("stops",))["stops"]
    where = {r["stop_id"]: (r["stop_lat"], r["stop_lon"]) for r in stops.iter_rows(named=True)}
    disturbed = _defect_trips(archive, {"gps_jump", "off_shape", "handover_duplicate"})
    distances = []
    for _, poll in feeds(archive):
        for f in poll.features:
            lp = f.properties.last_position
            if lp.state_position == "at_stop" and f.properties.trip.gtfs.trip_id not in disturbed:
                lat, lon = where[str(lp.last_stop.id)]
                lon_r, lat_r = f.geometry.coordinates
                assert lat_r is not None
                assert lon_r is not None
                distances.append(_metres(lat, lon, lat_r, lon_r))
    assert len(distances) > 1000
    assert sorted(distances)[int(0.99 * len(distances))] < 30  # GPS noise sd is 6 m


def test_reported_stop_delays_match_the_truth(archive: Path) -> None:
    events = {
        (e["trip_id"], e["stop_sequence"]): e["dep_delay_s"] for e in truth(archive, "stop_events")
    }
    checked: collections.Counter[str] = collections.Counter()
    for _, poll in feeds(archive):
        for f in poll.features:
            lp, trip = f.properties.last_position, f.properties.trip
            reported = lp.delay.last_stop_departure
            if reported is None:
                continue
            true = events[(trip.gtfs.trip_id, lp.last_stop.sequence)]
            if trip.agency_name.scheduled == "SYNTH REGIONAL":
                assert reported == round(true / 60) * 60
                checked["regional"] += 1
            else:
                assert reported == round(true)
                checked["city"] += 1
    assert checked["city"] > 100
    assert checked["regional"] > 100


def test_position_age_resembles_the_probe(archive: Path) -> None:
    ages = sorted(
        (r.fetched_at - f.properties.last_position.origin_timestamp).total_seconds()
        for r, poll in feeds(archive)
        for f in poll.features
    )
    median = ages[len(ages) // 2]
    assert 10 <= median <= 30  # probe: ~20 s
    assert ages[0] >= 0


def test_a_message_never_changes_under_the_same_origin_time(archive: Path) -> None:
    # A trip's last message is clamped to its arrival; it must not share a second with the
    # one before it. Trips with a planted handover duplicate carry a copied timestamp.
    copied = _defect_trips(archive, {"handover_duplicate"})
    seen: dict[tuple[str, datetime], str] = {}
    for _, poll in feeds(archive):
        for f in poll.features:
            trip = f.properties.trip.gtfs.trip_id
            if trip in copied:
                continue
            key = (trip, f.properties.last_position.origin_timestamp)
            assert seen.setdefault(key, f.model_dump_json()) == f.model_dump_json(), key


def test_a_trip_carries_at_most_one_defect_of_each_kind(archive: Path) -> None:
    # A second defect of the same kind would replace the first in the data but stay in truth.
    kinds = {"gps_jump", "off_shape", "silent_trip"}
    keys = [(d["kind"], d["trip_id"]) for d in truth(archive, "defects") if d["kind"] in kinds]
    assert len(keys) > 20
    assert len(keys) == len(set(keys))


def test_planted_collector_defects_are_in_the_manifest(archive: Path) -> None:
    rows = manifest_rows(archive)
    defects = truth(archive, "defects")
    kinds = collections.Counter(d["kind"] for d in defects)
    for kind in (
        "collection_gap",
        "api_error",
        "stale_feed",
        "gps_jump",
        "off_shape",
        "silent_trip",
        "handover_duplicate",
        "delay_bias",
    ):
        assert kinds[kind] > 0, kind

    times = [r.fetched_at for r in rows]
    for gap in (d for d in defects if d["kind"] == "collection_gap"):
        assert not [t for t in times if gap["start"] <= t <= gap["end"]]
    for error in (d for d in defects if d["kind"] == "api_error"):
        attempts = [
            r
            for r in rows
            if r.http_status == 503 and abs((r.fetched_at - error["start"]).total_seconds()) < 5
        ]
        assert [r.attempt for r in attempts] == [1, 2, 3]
    ok = [r for r in rows if r.http_status == 200]
    for stale in (d for d in defects if d["kind"] == "stale_feed"):
        inside = [
            r.sha256
            for r in ok
            if stale["start"] - timedelta(seconds=1)
            <= r.fetched_at
            <= stale["end"] + timedelta(seconds=1)
        ]
        assert len(set(inside)) == 1


def test_planted_trip_defects_are_visible(archive: Path) -> None:
    defects = truth(archive, "defects")
    polls = feeds(archive)
    duplicates = 0
    for _, poll in polls:
        keys = collections.Counter(
            (
                f.properties.trip.gtfs.route_type,
                f.properties.trip.vehicle_registration_number,
                f.properties.last_position.origin_timestamp,
            )
            for f in poll.features
        )
        duplicates += sum(1 for n in keys.values() if n > 1)
    planted = sum(1 for d in defects if d["kind"] == "handover_duplicate")
    assert planted > 0  # an earlier version planted none, so this check passed for nothing
    assert duplicates >= planted

    # A GPS jump is a single position far from where the vehicle was a poll earlier.
    jumps = {d["trip_id"]: d for d in defects if d["kind"] == "gps_jump"}
    seen_far = set()
    last_pos: dict[str, tuple[float, float]] = {}
    for _, poll in polls:
        for f in poll.features:
            trip = f.properties.trip.gtfs.trip_id
            lon, lat = f.geometry.coordinates
            assert lat is not None
            assert lon is not None
            if trip in last_pos and _metres(*last_pos[trip], lat, lon) > 700:
                seen_far.add(trip)
            last_pos[trip] = (lat, lon)
    assert seen_far & set(jumps)


def test_dst_change_is_generated_correctly(tmp_path: Path) -> None:
    cfg = small_cfg(start_date="2026-10-24", days=2)
    out = tmp_path / "dst"
    generate(cfg, out, start_at=datetime(2026, 10, 25, 0, 30, tzinfo=UTC), hours=1.0)
    offsets = {
        f.properties.last_position.origin_timestamp.utcoffset()
        for _, poll in feeds(out)
        for f in poll.features
    }
    assert offsets == {timedelta(hours=2), timedelta(hours=1)}  # CEST before 01:00 UTC, CET after
    # Night trips of service date 2026-10-24 are still running after the change.
    days = {e["service_date"] for e in truth(out, "stop_events")}
    assert date(2026, 10, 24) in days


def test_timetable_change_creates_a_second_gtfs_version(tmp_path: Path) -> None:
    cfg = small_cfg(days=3, timetable={"timetable_change_day": 1})
    out = tmp_path / "change"
    generate(cfg, out, start_at=datetime(2026, 9, 29, 5, 0, tzinfo=UTC), hours=0.3)
    versions = read_registry(out / "gtfs")
    assert [v.snapshot_date for v in versions] == [date(2026, 9, 28), date(2026, 9, 29)]
    assert resolve_gtfs(date(2026, 9, 28), out / "gtfs").version == versions[0]
    assert resolve_gtfs(date(2026, 9, 29), out / "gtfs").version == versions[1]
    trip_ids = {f.properties.trip.gtfs.trip_id for _, poll in feeds(out) for f in poll.features}
    assert any(t.startswith("L1_") and t.endswith("_v2") for t in trip_ids)
    assert not any(t.startswith("L2_") and t.endswith("_v2") for t in trip_ids)


def test_cli_synth(tmp_path: Path) -> None:
    out = tmp_path / "cli"
    args = [
        "synth",
        "--out",
        str(out),
        "--synth-config",
        str(CONFIG),
        "--start-at",
        "2026-09-28T05:00:00",
        "--hours",
        "0.1",
    ]
    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "SYNTHETIC archive" in first.output
    second = CliRunner().invoke(app, args)
    assert second.exit_code == 1
    assert "not empty" in second.output


def test_truth_tables_are_typed(archive: Path) -> None:
    events = pl.read_parquet(archive / "truth" / "stop_events.parquet")
    assert events.schema["true_arr"] == pl.Datetime("ms", "UTC")
    assert events.select((pl.col("true_dep") >= pl.col("true_arr")).all()).item()
    segments = pl.read_parquet(archive / "truth" / "segments.parquet")
    assert segments["bottleneck"].sum() > 0
    assert segments["padded"].sum() > 0
