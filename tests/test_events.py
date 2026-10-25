"""Tests for layer L3 (stop events): the algorithm, the pipeline and its accuracy."""

import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from headway.cli import app
from headway.config import load_settings
from headway.events.compare import nearest_reported, stop_position, summarize
from headway.events.config import EventsConfig, load_events_config
from headway.events.findings import write_findings
from headway.events.pipeline import (
    EventsInputs,
    _stop_distances,
    _Stops,
    _stops_by_trip,
    events_inputs_for,
    reconstruct_date,
)
from headway.events.project import (
    cumulative_length,
    isotonic_increasing,
    project_along,
)
from headway.events.reconstruct import find_plateaus, reconstruct_events
from headway.gtfs.timeconv import gtfs_time_to_utc
from headway.ingest.pipeline import ingest_dates
from headway.synth import SynthConfig, generate, load_synth_config

REPO = Path(__file__).resolve().parents[1]
SETTINGS = load_settings(REPO / "config" / "default.toml")
CFG = load_events_config(REPO / "config" / "events.toml")
DAY = date(2026, 9, 28)
WINDOW_START = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)  # 07:00 local, morning peak

runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})


# --- the algorithm on hand-made drives --------------------------------------------------------


def drive(t: float) -> float:
    """10 m/s, standing at 1000 m from t=100 to t=130 (arrival 100, departure 130)."""
    if t < 100:
        return 10 * t
    return 1000.0 if t < 130 else 1000 + 10 * (t - 130)


STOPS = np.array([500.0, 1000.0, 1500.0])  # a pass, a stop, a pass


def run(step: int, offset: int) -> dict[int, Any]:
    t = np.arange(offset, 300, step, dtype=float)
    d = np.array([drive(x) for x in t])
    return {e.stop_index: e for e in reconstruct_events(t, d, STOPS, 30.0, 20.0)}


@pytest.mark.parametrize("step", [20, 60])
def test_a_noise_free_drive_is_reconstructed_within_its_uncertainty_at_every_phase(
    step: int,
) -> None:
    checked = 0
    for offset in range(step):
        stop = run(step, offset).get(1)
        if stop is None:
            continue
        for estimate, uncertainty, truth in (
            (stop.arr, stop.arr_uncert, 100.0),
            (stop.dep, stop.dep_uncert, 130.0),
        ):
            if estimate is None or uncertainty is None:
                continue
            checked += 1
            assert abs(estimate - truth) <= uncertainty + 1e-9, (step, offset)
    assert checked >= step  # the sweep did not skip everything


def test_a_stop_seen_standing_gives_arrival_and_departure_from_the_plateau() -> None:
    stop = run(20, 0)[1]
    assert stop.method == "plateau"
    assert (stop.arr, stop.dep) == (pytest.approx(100.0), pytest.approx(130.0))
    assert stop.arr_uncert == 10.0  # half of the 20 s between positions


def test_a_stop_that_was_not_seen_is_interpolated() -> None:
    passed = run(20, 0)[0]
    assert passed.method == "interpolated"
    assert passed.arr == passed.dep == pytest.approx(50.0)


def test_a_vehicle_already_standing_at_the_first_stop_has_no_arrival() -> None:
    t = np.arange(0, 100, 20, dtype=float)
    d = np.array([0.0, 2.0, 1.0, 3.0, 150.0])  # waits at the terminal, then leaves
    first = reconstruct_events(t, d, np.array([0.0, 150.0]), 30.0, 20.0)[0]
    assert first.arr is None
    assert first.dep is not None
    assert 60 <= first.dep <= 80


def test_a_vehicle_still_standing_at_the_last_stop_has_no_departure() -> None:
    t = np.arange(0, 120, 20, dtype=float)
    d = np.array([0.0, 100.0, 200.0, 300.0, 301.0, 299.0])
    last = reconstruct_events(t, d, np.array([300.0]), 30.0, 20.0)[0]
    assert last.dep is None
    assert last.arr is not None


def test_stops_the_positions_do_not_reach_get_no_event() -> None:
    t = np.arange(0, 100, 20, dtype=float)
    d = np.array([200.0, 300.0, 400.0, 500.0, 600.0])
    events = reconstruct_events(t, d, np.array([50.0, 400.0, 900.0]), 30.0, 20.0)
    assert [e.stop_index for e in events] == [1]


def test_standing_away_from_a_stop_is_not_a_stop() -> None:
    t = np.arange(0, 200, 20, dtype=float)
    d = np.array([0, 100, 200, 300, 300, 300, 300, 400, 500, 600], dtype=float)  # a queue at 300 m
    events = reconstruct_events(t, d, np.array([450.0]), 30.0, 20.0)
    assert events[0].method == "interpolated"


def test_gps_noise_does_not_split_a_long_stop() -> None:
    rng = np.random.default_rng(1)
    t = np.arange(0, 400, 10, dtype=float)
    true = np.where(t < 100, 10 * t, np.where(t < 300, 1000.0, 1000 + 10 * (t - 300)))
    d = true + rng.normal(0, 6, size=len(t))  # 6 m of GPS noise
    stop = reconstruct_events(t, d, np.array([1000.0]), 30.0, 20.0)[0]
    assert stop.method == "plateau"
    assert stop.arr is not None
    assert stop.dep is not None
    assert abs(stop.arr - 100) <= 12
    assert abs(stop.dep - 300) <= 12  # the whole 200 s stop, not the first piece of it
    assert stop.n_positions >= 18


def test_runs_of_positions_at_one_place() -> None:
    d = np.array([0, 100, 101, 103, 102, 200, 300, 301], dtype=float)
    assert find_plateaus(d, 20.0) == [(0, 0), (1, 4), (5, 5), (6, 7)]
    # Neighbouring runs whose centres are within the tolerance are one place.
    assert find_plateaus(np.array([0, 10, 21, 25], dtype=float), 20.0) == [(0, 3)]
    # A vehicle creeping 30 m per position is moving, not standing.
    assert find_plateaus(np.array([0, 30, 60, 90], dtype=float), 20.0) == [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 3),
    ]


def test_isotonic_regression_averages_dips_instead_of_ratcheting() -> None:
    np.testing.assert_allclose(
        isotonic_increasing(np.array([1.0, 3.0, 2.0, 4.0])), [1, 2.5, 2.5, 4]
    )
    sorted_input = np.array([1.0, 2.0, 2.0, 5.0])
    np.testing.assert_allclose(isotonic_increasing(sorted_input), sorted_input)
    spike = isotonic_increasing(np.array([0.0, 10.0, 100.0, 20.0, 30.0, 40.0]))
    assert np.all(np.diff(spike) >= 0)
    assert spike[-1] < 60  # a running maximum would have kept 100 for every later position


# --- loops and revisited stops ---------------------------------------------------------------


LOOP = np.array([[0, 0], [1000, 0], [1000, 1000], [0, 1000], [0, 0]], dtype=float)


def test_the_api_distance_tells_the_two_passes_of_a_loop_apart() -> None:
    cumulative = cumulative_length(LOOP)
    start = np.array([[0.0, 0.0]])
    first, _ = project_along(start, LOOP, cumulative, np.array([10.0]), 500.0, 50.0)
    second, _ = project_along(start, LOOP, cumulative, np.array([3990.0]), 500.0, 50.0)
    assert first[0] == pytest.approx(0.0, abs=1)
    assert second[0] == pytest.approx(4000.0, abs=1)


def test_a_wrong_api_distance_falls_back_to_the_whole_shape() -> None:
    cumulative = cumulative_length(LOOP)
    point = np.array([[1000.0, 500.0]])
    along, offset = project_along(point, LOOP, cumulative, np.array([3500.0]), 300.0, 50.0)
    assert along[0] == pytest.approx(1500.0, abs=1)
    assert offset[0] == pytest.approx(0.0, abs=1e-6)


def loop_stops(distances_km: list[float]) -> _Stops:
    ids = ["A", "B", "C", "D", "A"]  # the loop starts and ends at stop A
    nan = np.full(5, np.nan)
    return _Stops([1, 2, 3, 4, 5], ids, np.array(distances_km) * 1000.0, nan, nan)


STOP_XY = {
    "A": np.array([0.0, 0.0]),
    "B": np.array([1000.0, 0.0]),
    "C": np.array([1000.0, 1000.0]),
    "D": np.array([0.0, 1000.0]),
}


def test_a_revisited_stop_keeps_its_timetable_distances() -> None:
    stops = loop_stops([0, 1, 2, 3, 4])
    distances, why = _stop_distances(stops, LOOP, cumulative_length(LOOP), STOP_XY, CFG)
    assert why == ""
    assert distances is not None
    np.testing.assert_allclose(distances, [0, 1000, 2000, 3000, 4000])


def test_a_loop_without_timetable_distances_is_excluded_with_a_reason() -> None:
    stops = loop_stops([np.nan] * 5)
    distances, why = _stop_distances(stops, LOOP, cumulative_length(LOOP), STOP_XY, CFG)
    assert distances is None
    assert "stop order" in why
    assert "loop" in why


def test_a_loop_is_reconstructed_end_to_end_with_both_visits_of_the_start() -> None:
    cumulative = cumulative_length(LOOP)
    t = np.arange(0, 480, 20, dtype=float)
    along = np.minimum(t * 10.0, 4000.0)  # reaches the end of the loop at t=400
    xy = np.array([_point_at(LOOP, cumulative, a) for a in along])
    hints = along.copy()
    projected, _ = project_along(xy, LOOP, cumulative, hints, 500.0, 50.0)
    events = reconstruct_events(t, projected, np.array([0.0, 1000, 2000, 3000, 4000]), 30.0, 20.0)
    times = {e.stop_index: e for e in events}
    assert times[4].arr == pytest.approx(400.0, abs=21)  # the second visit of the start stop
    assert times[0].dep is not None
    assert times[0].dep < 50  # the first visit, 4000 m earlier


def _point_at(line: Any, cumulative: Any, along: float) -> list[float]:
    i = min(int(np.searchsorted(cumulative, along, side="right")) - 1, len(line) - 2)
    frac = (along - cumulative[i]) / (cumulative[i + 1] - cumulative[i])
    return list(line[i] + frac * (line[i + 1] - line[i]))


# --- schedule times ---------------------------------------------------------------------------


def test_schedule_times_use_the_service_day_reference_across_the_dst_change() -> None:
    # Summer time ends on 2026-10-25: the service day is 25 h long, and times pass 24:00.
    frame = pl.DataFrame(
        {
            "trip_id": ["t"] * 3,
            "stop_id": ["a", "b", "c"],
            "stop_sequence": [1, 2, 3],
            "arrival_time": ["23:50:00", "25:13:00", None],
            "departure_time": ["23:51:00", "25:14:00", ""],
            "shape_dist_traveled": [0.0, 1.0, 2.0],
        }
    )
    stops = _stops_by_trip(frame, date(2026, 10, 25), "Europe/Prague")["t"]
    for got, text in zip(stops.sched_arr[:2], ["23:50:00", "25:13:00"], strict=True):
        assert got == gtfs_time_to_utc(date(2026, 10, 25), text).timestamp()
    assert np.isnan(stops.sched_arr[2])
    assert np.isnan(stops.sched_dep[2])
    # GTFS times count elapsed time from the day's reference, so the repeated hour of the
    # change does not add or remove anything between two times.
    assert stops.sched_arr[1] - stops.sched_arr[0] == 83 * 60


# --- comparison helpers ----------------------------------------------------------------------


def test_the_nearest_reported_delay_must_be_close_in_time() -> None:
    times = np.array([0.0, 20.0, 40.0])
    reported = np.array([10.0, np.nan, 30.0])
    assert nearest_reported(times, reported, 38.0, 30.0) == (30.0, 2.0)
    # Only the two neighbours of the event count, and a missing delay is skipped.
    assert nearest_reported(times, reported, 21.0, 30.0) == (30.0, 19.0)
    assert nearest_reported(times, reported, 200.0, 30.0) is None


def test_stop_positions() -> None:
    assert [stop_position(i, 4) for i in range(4)] == ["first", "middle", "middle", "last"]


def test_the_summary_has_a_sample_size_on_every_row() -> None:
    frame = pl.DataFrame(
        {
            "route_type": [0, 0, 3],
            "stop_position": ["first", "middle", "middle"],
            "route_id": ["a", "a", "b"],
            "diff_s": [1.0, 3.0, 50.0],
        }
    )
    summary = summarize(frame)
    assert summary["n"] == 3
    assert all(
        "n" in row
        for group in ("by_route_type", "by_stop_position", "by_route")
        for row in summary[group]
    )
    assert summarize(frame.head(0)) == {"n": 0}


# --- the pipeline on synthetic archives -------------------------------------------------------


def synth_cfg(report_interval: tuple[float, float] | None, *, defects: bool = False) -> SynthConfig:
    """A small network without data defects.

    With ``report_interval`` the vehicles report at a fixed spacing and the API publishes at
    once, so the sampling is exactly that; without it the realistic spacing (5-30 s) and the
    publication lag of the source configuration are kept.
    """
    base = load_synth_config(REPO / "config" / "synth.toml").model_dump()
    base.update(days=2)
    base["network"].update(tram_routes=2, bus_routes=2)
    base["timetable"].update(timetable_change_day=0)
    if report_interval is not None:
        base["reporting"].update(
            report_interval_s=list(report_interval), publish_lag_s=0.05, long_lag_probability=0.0
        )
    if not defects:
        for key in base["defects"]:
            if key.startswith("handover") or isinstance(base["defects"][key], float):
                base["defects"][key] = 0.0
        base["defects"].update(collection_gap_min=[1.0, 2.0])
    return SynthConfig.model_validate(base)


def build(root: Path, cfg: SynthConfig) -> EventsInputs:
    generate(cfg, root, start_at=WINDOW_START, hours=1.0)
    inputs = events_inputs_for(root, SETTINGS, CFG)
    ingest_dates([DAY], inputs.ingest)
    return inputs


@pytest.fixture(scope="module")
def sampled_20(tmp_path_factory: pytest.TempPathFactory) -> EventsInputs:
    return build(tmp_path_factory.mktemp("ev20") / "a", synth_cfg((20.0, 20.0)))


@pytest.fixture(scope="module")
def sampled_60(tmp_path_factory: pytest.TempPathFactory) -> EventsInputs:
    return build(tmp_path_factory.mktemp("ev60") / "a", synth_cfg((60.0, 60.0)))


@pytest.fixture(scope="module")
def realistic(tmp_path_factory: pytest.TempPathFactory) -> EventsInputs:
    return build(tmp_path_factory.mktemp("evreal") / "a", synth_cfg(None))


def events_of(inputs: EventsInputs) -> pl.DataFrame:
    return pl.read_parquet(inputs.events_dir / f"service_date={DAY}" / "part-0.parquet")


def truth_of(inputs: EventsInputs) -> pl.DataFrame:
    root = inputs.ingest.positions_dir.parent.parent
    return pl.read_parquet(root / "truth" / "stop_events.parquet").select(
        "trip_id", "stop_sequence", "true_arr", "true_dep"
    )


def within_uncertainty(
    inputs: EventsInputs, time_col: str, unc_col: str, truth_col: str
) -> tuple[int, float]:
    joined = events_of(inputs).join(truth_of(inputs), on=["trip_id", "stop_sequence"])
    sub = joined.filter(pl.col(time_col).is_not_null() & pl.col(unc_col).is_not_null())
    error = (sub[time_col] - sub[truth_col]).dt.total_milliseconds().to_numpy() / 1000
    return len(error), float((np.abs(error) <= sub[unc_col].to_numpy() + 1e-9).mean())


@pytest.mark.parametrize("fixture", ["sampled_20", "sampled_60", "realistic"])
@pytest.mark.parametrize(
    ("time_col", "unc_col", "truth_col"),
    [("arr_ts", "uncert_s", "true_arr"), ("dep_ts", "dep_uncert_s", "true_dep")],
)
def test_reconstructed_times_are_within_their_uncertainty_for_at_least_95_percent(
    fixture: str, time_col: str, unc_col: str, truth_col: str, request: pytest.FixtureRequest
) -> None:
    inputs: EventsInputs = request.getfixturevalue(fixture)
    reconstruct_date(DAY, inputs)
    n, share = within_uncertainty(inputs, time_col, unc_col, truth_col)
    assert n >= 800, f"too few events to judge: {n}"
    assert share >= 0.95, (fixture, time_col, share)


def test_the_stated_uncertainty_is_informative_not_just_wide(sampled_20: EventsInputs) -> None:
    reconstruct_date(DAY, sampled_20)
    joined = events_of(sampled_20).join(truth_of(sampled_20), on=["trip_id", "stop_sequence"])
    sub = joined.filter(pl.col("arr_ts").is_not_null())
    error = np.abs((sub["arr_ts"] - sub["true_arr"]).dt.total_milliseconds().to_numpy() / 1000)
    assert float(np.median(error)) < 0.5 * float(np.median(sub["uncert_s"].to_numpy()))


def test_both_methods_occur_at_sparse_sampling(sampled_60: EventsInputs) -> None:
    report = reconstruct_date(DAY, sampled_60)
    assert report["events_by_method"].get("plateau", 0) > 100
    assert report["events_by_method"].get("interpolated", 0) > 100


def test_the_report_counts_what_was_done(realistic: EventsInputs) -> None:
    report = reconstruct_date(DAY, realistic)
    events = events_of(realistic)
    assert report["events"] == events.height
    assert report["synthetic"] is True
    assert (
        report["trips_reconstructed"] + report["trips_excluded"] == report["trips_with_positions"]
    )
    assert report["trips_reconstructed"] > 100
    assert 0 < report["event_coverage"] <= 1
    assert report["comparison"]["n"] > 1000
    assert set(events["method"].unique()) <= {"plateau", "interpolated"}
    assert events["delay_s"].null_count() < events.height * 0.05


def test_the_planted_bias_of_the_reported_delay_is_recovered(realistic: EventsInputs) -> None:
    report = reconstruct_date(DAY, realistic)
    by_route = {row["route_id"]: row for row in report["comparison"]["by_route"]}
    bias_route = json.loads(
        (realistic.ingest.positions_dir.parent.parent / "SYNTHETIC.json").read_text(
            encoding="utf-8"
        )
    )["delay_bias_route"]
    planted = load_synth_config(REPO / "config" / "synth.toml").reporting.golemio_delay_bias_s
    assert by_route[bias_route]["n"] >= 100
    assert by_route[bias_route]["median_diff_s"] == pytest.approx(planted, abs=10)
    others = [row for route, row in by_route.items() if route != bias_route]
    assert others
    for row in others:
        assert abs(row["median_diff_s"]) < 10, row


def test_an_event_can_be_traced_to_a_stop_of_the_timetable(realistic: EventsInputs) -> None:
    reconstruct_date(DAY, realistic)
    events = events_of(realistic)
    sha = realistic.ingest.gtfs_dir
    assert events["gtfs_sha256"].null_count() == 0
    assert (events["stop_dist_m"] >= 0).all()
    ordered = events.sort("trip_id", "stop_sequence")
    assert ordered.select(
        (pl.col("stop_dist_m").diff().over("trip_id") >= 0).fill_null(True).all()
    ).item()
    assert sha.exists()


def test_reconstruction_is_idempotent(realistic: EventsInputs) -> None:
    reconstruct_date(DAY, realistic)
    first = pq.read_table(realistic.events_dir / f"service_date={DAY}" / "part-0.parquet")
    reconstruct_date(DAY, realistic)
    second = pq.read_table(realistic.events_dir / f"service_date={DAY}" / "part-0.parquet")
    assert first.equals(second)


def test_the_findings_page_is_labelled_and_every_row_has_a_sample_size(
    realistic: EventsInputs,
) -> None:
    reconstruct_date(DAY, realistic)
    page = write_findings(realistic)
    text = page.read_text(encoding="utf-8")
    assert "SYNTHETIC DATA" in text
    for heading in ("## By route", "## By vehicle type", "## By stop position", "## Limits"):
        assert heading in text
    assert "How accurate is the reconstruction? (synthetic archive only)" in text
    for line in text.splitlines():
        if line.startswith("| L"):  # a route row: id, n, median, 10th, 90th
            assert int(line.split("|")[2]) > 0
    for chart in ("by-route.png", "by-stop-position.png"):
        assert (page.parent / chart).stat().st_size > 5_000
        assert f"]({chart})" in text


def test_the_findings_page_says_so_when_there_is_nothing_to_compare(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    (root / "raw" / "manifest").mkdir(parents=True)
    page = write_findings(events_inputs_for(root, SETTINGS, CFG))
    assert "No reconstructed event could be compared" in page.read_text(encoding="utf-8")


# --- exclusions are explicit --------------------------------------------------------------------


def test_every_excluded_trip_has_a_reason(realistic: EventsInputs) -> None:
    strict = EventsConfig.model_validate({**CFG.model_dump(), "trip": {"min_positions": 10_000}})
    inputs = EventsInputs(
        realistic.ingest,
        realistic.events_dir.parent / "ev_strict",
        realistic.exclusions_dir.parent / "ex_strict",
        realistic.compare_dir.parent / "cm_strict",
        realistic.reports_dir.parent / "rp_strict",
        strict,
    )
    report = reconstruct_date(DAY, inputs)
    assert report["events"] == 0
    assert report["exclusion_reasons"] == {"too_few_positions": report["trips_with_positions"]}
    table = pq.read_table(inputs.exclusions_dir / f"service_date={DAY}" / "part-0.parquet")
    assert table.num_rows == report["trips_with_positions"] > 100
    assert set(table.column("detail").to_pylist()) != {""}


def test_a_missing_timetable_is_reported_not_guessed(
    realistic: EventsInputs, tmp_path: Path
) -> None:
    root = tmp_path / "no_gtfs"
    source = realistic.ingest.positions_dir.parent.parent
    shutil.copytree(source, root, ignore=shutil.ignore_patterns("data", "gtfs"))
    inputs = events_inputs_for(root, SETTINGS, CFG)
    ingest_dates([DAY], inputs.ingest)
    report = reconstruct_date(DAY, inputs)
    assert report["events"] == 0
    assert any("No timetable" in n for n in report["notes"])


def test_a_day_without_positions_is_reported(realistic: EventsInputs, tmp_path: Path) -> None:
    root = tmp_path / "not_ingested"
    shutil.copytree(
        realistic.ingest.positions_dir.parent.parent, root, ignore=shutil.ignore_patterns("data")
    )
    report = reconstruct_date(DAY, events_inputs_for(root, SETTINGS, CFG))
    assert report["events"] == 0
    assert any("No positions" in n for n in report["notes"])


# --- CLI --------------------------------------------------------------------------------------


def test_cli_reconstructs_one_date_and_all_dates(
    realistic: EventsInputs, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "copy"
    shutil.copytree(
        realistic.ingest.positions_dir.parent.parent, root, ignore=shutil.ignore_patterns("data")
    )
    monkeypatch.chdir(REPO)
    assert runner.invoke(app, ["ingest", "--all", "--archive", str(root)]).exit_code == 0
    one = runner.invoke(app, ["events", "--date", "2026-09-28", "--archive", str(root)])
    assert one.exit_code == 0, one.output
    assert "SYNTHETIC 2026-09-28:" in one.output
    assert "events from" in one.output
    every = runner.invoke(app, ["events", "--all", "--archive", str(root)])
    assert every.exit_code == 0, every.output
    assert every.output.count("SYNTHETIC 2026-") == 1


@pytest.mark.parametrize("args", [[], ["--date", "2026-09-28", "--all"], ["--date", "soon"]])
def test_cli_rejects_bad_arguments(
    args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)
    assert runner.invoke(app, ["events", "--archive", str(tmp_path), *args]).exit_code == 2


def test_cli_reports_an_archive_without_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)
    result = runner.invoke(app, ["events", "--all", "--archive", str(tmp_path)])
    assert result.exit_code == 1
    assert "no manifest records" in result.output
