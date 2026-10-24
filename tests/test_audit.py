"""Tests for layer L2 (audit): detectors, thresholds, the report and the CLI."""

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from headway.audit.collection import collection_findings
from headway.audit.config import AuditConfig, load_audit_config
from headway.audit.findings import CATEGORIES
from headway.audit.geometry import distance_to_polyline
from headway.audit.pipeline import AuditInputs, audit_date, audit_inputs_for
from headway.audit.positions import _jump_findings, runs
from headway.audit.report import _clock, render_markdown
from headway.cli import app
from headway.collect import PollRecord
from headway.config import load_settings
from headway.ingest.pipeline import ingest_dates
from headway.synth import SynthConfig, generate, load_synth_config

REPO = Path(__file__).resolve().parents[1]
SETTINGS = load_settings(REPO / "config" / "default.toml")
CFG = load_audit_config(REPO / "config" / "audit.toml")
DAY = date(2026, 9, 28)
WINDOW_START = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)  # 07:00 local, morning peak
WINDOW_HOURS = 1.0

runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})


def synth_cfg(*, planted: bool) -> SynthConfig:
    """A small network in which every message is observed, with or without defects.

    Messages are 25-30 s apart and published at once, so each one is the newest message at
    some 20 s poll. Nothing planted can then hide between two polls, and a miss is a miss
    of the audit.
    """
    base = load_synth_config(REPO / "config" / "synth.toml").model_dump()
    base.update(days=2)
    base["network"].update(tram_routes=2, bus_routes=2)
    base["timetable"].update(timetable_change_day=0)
    base["reporting"].update(
        report_interval_s=[25.0, 30.0], publish_lag_s=0.05, long_lag_probability=0.0
    )
    rates = dict(
        collection_gaps=40.0,
        api_errors=60.0,
        stale_episodes=40.0,
        gps_jumps=150.0,
        off_shape=150.0,
        silent_trips=150.0,
        handover_duplicate_probability=0.5,
    )
    base["defects"].update(rates if planted else {k: 0.0 for k in rates})
    base["defects"].update(collection_gap_min=[1.0, 2.0])
    return SynthConfig.model_validate(base)


def build(root: Path, *, planted: bool) -> AuditInputs:
    generate(synth_cfg(planted=planted), root, start_at=WINDOW_START, hours=WINDOW_HOURS)
    inputs = audit_inputs_for(root, SETTINGS, CFG)
    ingest_dates([DAY], inputs.ingest)
    return inputs


@pytest.fixture(scope="module")
def planted(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, AuditInputs]:
    root = tmp_path_factory.mktemp("audit_planted") / "archive"
    return root, build(root, planted=True)


@pytest.fixture(scope="module")
def clean(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, AuditInputs]:
    root = tmp_path_factory.mktemp("audit_clean") / "archive"
    return root, build(root, planted=False)


def read_findings(inputs: AuditInputs) -> list[dict[str, Any]]:
    path = inputs.audit_dir / f"service_date={DAY}" / "findings.parquet"
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows


def truth(root: Path, kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = pq.read_table(root / "truth" / "defects.parquet").to_pylist()
    return [r for r in rows if r["kind"] == kind]


def in_window(defect: dict[str, Any], *, tail: timedelta = timedelta(0)) -> bool:
    """True if the defect starts inside the polled hour (with ``tail`` to spare at the end)."""
    end = WINDOW_START + timedelta(hours=WINDOW_HOURS)
    return bool(WINDOW_START + timedelta(minutes=1) <= defect["start"] <= end - tail)


def overlapping(
    found: list[dict[str, Any]], defect: dict[str, Any], category: str, slack_s: float = 0.0
) -> list[dict[str, Any]]:
    slack = timedelta(seconds=slack_s)
    return [
        f
        for f in found
        if f["category"] == category
        and f["start_ts"] <= defect["end"] + slack
        and f["end_ts"] >= defect["start"] - slack
        and (not defect["trip_id"] or f["entity"] == defect["trip_id"])
    ]


# --- planted defects are detected, and nothing else is reported -------------------------


def collector_spans(root: Path) -> list[tuple[datetime, datetime]]:
    """Time spans in which a planted collector defect hides what the vehicles report."""
    pad = timedelta(seconds=45)
    return [
        (d["start"] - pad, d["end"] + pad)
        for kind in ("collection_gap", "api_error", "stale_feed")
        for d in truth(root, kind)
    ]


def covered_s(defect: dict[str, Any], spans: list[tuple[datetime, datetime]]) -> float:
    """Seconds of the defect that lie inside the collector spans (spans do not overlap much)."""
    total = 0.0
    cursor = defect["start"]
    for a, b in sorted(spans):
        lo, hi = max(a, cursor), min(b, defect["end"])
        if hi > lo:
            total += (hi - lo).total_seconds()
            cursor = hi
    return total


def observable(root: Path, defect: dict[str, Any], *, silent: bool = False) -> bool:
    """True if the data can hold evidence of the planted defect.

    A jump, an off-route stretch or a handover inside a collector outage is invisible, and
    so is a silence that the outage almost fills.
    """
    spans = collector_spans(root)
    if silent:
        duration = (defect["end"] - defect["start"]).total_seconds()
        if duration - covered_s(defect, spans) < 360:
            return False
        # A silence that runs to the end of the trip is ambiguous when an outage starts soon
        # after it: the trip may simply have finished unobserved inside the outage.
        positions = pl.read_parquet(
            root / "data" / "positions" / f"service_date={DAY}" / "part-0.parquet"
        ).filter(pl.col("trip_id") == defect["trip_id"])
        returns = positions.filter(pl.col("origin_ts") > defect["end"]).height > 0
        near_outage = any(
            defect["start"] - timedelta(seconds=60) <= a <= defect["start"] + timedelta(seconds=330)
            for a, _ in spans
        )
        return returns or not near_outage
    if any(a <= defect["end"] and b >= defect["start"] for a, b in spans):
        return False
    # Reports in a planted silent period are never sent, so nothing in it can be seen.
    pad = timedelta(seconds=30)
    return not any(
        d["trip_id"] == defect["trip_id"]
        and d["start"] - pad <= defect["end"]
        and d["end"] + pad >= defect["start"]
        for d in truth(root, "silent_trip")
        if defect["kind"] != "silent_trip"
    )


@pytest.mark.parametrize(
    ("kind", "category", "slack_s", "tail_min"),
    [
        ("collection_gap", "collection_gap", 0, 0),
        ("api_error", "api_errors", 30, 0),
        ("stale_feed", "stale_feed", 30, 0),
        ("gps_jump", "gps_jump", 30, 0),
        ("off_shape", "off_shape", 0, 0),
        ("silent_trip", "silent_trip", 0, 7),
    ],
)
def test_every_observable_planted_defect_is_detected(
    planted: tuple[Path, AuditInputs], kind: str, category: str, slack_s: float, tail_min: int
) -> None:
    root, inputs = planted
    audit_date(DAY, inputs)
    found = read_findings(inputs)
    in_hour = [d for d in truth(root, kind) if in_window(d, tail=timedelta(minutes=tail_min))]
    if kind in ("collection_gap", "api_error", "stale_feed"):
        expected = in_hour
    else:
        expected = [d for d in in_hour if observable(root, d, silent=kind == "silent_trip")]
    floor = 2 if kind in ("api_error", "stale_feed") else 3
    assert len(expected) >= floor, f"too few observable planted {kind}: {len(expected)}"
    missed = [
        (d["trip_id"], str(d["start"]), d["details"])
        for d in expected
        if not overlapping(found, d, category, slack_s)
    ]
    assert not missed, missed


def test_every_observable_planted_handover_duplicate_is_detected(
    planted: tuple[Path, AuditInputs],
) -> None:
    root, inputs = planted
    audit_date(DAY, inputs)
    found = [f for f in read_findings(inputs) if f["category"] == "vehicle_on_two_trips"]
    defects = [d for d in truth(root, "handover_duplicate") if in_window(d) and observable(root, d)]
    assert len(defects) >= 3
    for d in defects:
        near = [
            f
            for f in found
            if d["trip_id"] in f["details"]
            and abs((f["start_ts"] - d["start"]).total_seconds()) < 90
        ]
        assert near, d


def touches(finding: dict[str, Any], defect: dict[str, Any], slack_s: float = 30.0) -> bool:
    """True if the finding spans the defect in time (and belongs to its trip, if it has one)."""
    slack = timedelta(seconds=slack_s)
    same_time = (
        finding["start_ts"] <= defect["end"] + slack
        and finding["end_ts"] >= defect["start"] - slack
    )
    text = f"{finding['entity']} {finding['details']}"
    same_trip = not defect["trip_id"] or defect["trip_id"] in text
    return bool(same_time and same_trip)


def test_nothing_is_reported_that_was_not_planted(planted: tuple[Path, AuditInputs]) -> None:
    root, inputs = planted
    audit_date(DAY, inputs)
    truth_kind = {"api_errors": "api_error", "vehicle_on_two_trips": "handover_duplicate"}
    for finding in read_findings(inputs):
        category = finding["category"]
        assert category in {
            "collection_gap",
            "api_errors",
            "stale_feed",
            "gps_jump",
            "off_shape",
            "silent_trip",
            "vehicle_on_two_trips",
        }, finding
        causes = truth(root, truth_kind.get(category, category))
        if category == "off_shape":
            causes += truth(root, "gps_jump")  # a GPS jump also throws a position off the route
        assert any(touches(finding, d) for d in causes), finding


def test_a_clean_day_has_no_findings(clean: tuple[Path, AuditInputs]) -> None:
    _, inputs = clean
    summary = audit_date(DAY, inputs)
    assert read_findings(inputs) == []
    assert summary["findings_total"] == 0
    assert summary["collection"]["coverage"] == 1.0
    assert summary["positions"]["fresh_share"] == 1.0
    assert summary["positions"]["on_shape_share"] == 1.0
    assert "No findings" in (inputs.audit_dir / f"service_date={DAY}" / "summary.md").read_text(
        encoding="utf-8"
    )


# --- summary, report and files --------------------------------------------------------------


def test_the_summary_counts_the_findings(planted: tuple[Path, AuditInputs]) -> None:
    _, inputs = planted
    summary = audit_date(DAY, inputs)
    found = read_findings(inputs)
    assert summary["findings_total"] == len(found)
    for category in CATEGORIES:
        assert summary["findings"][category]["count"] == sum(
            f["category"] == category for f in found
        )
        assert summary["findings"][category]["severity"] == CFG.severity[category]
    assert {f["severity"] for f in found} <= {"error", "warning", "info"}
    assert summary["synthetic"] is True
    assert 0 < summary["collection"]["coverage"] < 1
    assert summary["thresholds"]["positions"]["off_shape_m"] == CFG.positions.off_shape_m


def test_the_markdown_is_readable_without_the_code(planted: tuple[Path, AuditInputs]) -> None:
    _, inputs = planted
    audit_date(DAY, inputs)
    text = (inputs.audit_dir / f"service_date={DAY}" / "summary.md").read_text(encoding="utf-8")
    for heading in (
        "# Data-quality audit for service date 2026-09-28",
        "## At a glance",
        "## Findings by category",
        "## Thresholds used",
        "## Limits of this audit",
    ):
        assert heading in text
    assert "SYNTHETIC DATA" in text
    for category, meaning in CATEGORIES.items():
        assert f"| {category} |" in text
        assert meaning in text
    assert "50.0 m" in text  # the thresholds are printed with their values
    assert "Polls that succeeded" in text


def test_the_audit_is_idempotent(planted: tuple[Path, AuditInputs]) -> None:
    _, inputs = planted
    folder = inputs.audit_dir / f"service_date={DAY}"
    audit_date(DAY, inputs)
    first = {p.name: p.read_bytes() for p in folder.iterdir()}
    audit_date(DAY, inputs)
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == first
    assert set(first) == {"findings.parquet", "summary.json", "summary.md"}


def test_a_missing_timetable_is_reported_not_guessed(
    planted: tuple[Path, AuditInputs], tmp_path: Path
) -> None:
    root = tmp_path / "no_gtfs"
    shutil.copytree(planted[0], root, ignore=shutil.ignore_patterns("data", "gtfs"))
    inputs = audit_inputs_for(root, SETTINGS, CFG)
    ingest_dates([DAY], inputs.ingest)
    summary = audit_date(DAY, inputs)
    assert not summary["positions"]["timetable_available"]
    assert any("No timetable" in n for n in summary["notes"])
    assert {f["category"] for f in read_findings(inputs)}.isdisjoint({"off_shape", "unknown_trip"})


def test_a_day_without_positions_is_reported(
    planted: tuple[Path, AuditInputs], tmp_path: Path
) -> None:
    root = tmp_path / "not_ingested"
    shutil.copytree(planted[0], root, ignore=shutil.ignore_patterns("data"))
    inputs = audit_inputs_for(root, SETTINGS, CFG)
    summary = audit_date(DAY, inputs)
    assert summary["positions"]["positions"] == 0
    assert any("No positions were ingested" in n for n in summary["notes"])
    assert summary["findings"]["collection_gap"]["count"] >= 1  # the manifest is still audited


# --- detectors on hand-made data ------------------------------------------------------------


T0 = datetime(2026, 10, 24, 10, 0, 0, tzinfo=UTC)


def poll(seconds: int, *, ok: bool = True, sha: str | None = None, attempt: int = 1) -> PollRecord:
    return PollRecord(
        fetched_at=T0 + timedelta(seconds=seconds),
        attempt=attempt,
        http_status=200 if ok else 503,
        sha256=sha or f"sha-{seconds}",
        path=f"p{seconds}" if ok else None,
    )


def test_a_missing_stretch_is_a_gap_with_an_unrecorded_cause() -> None:
    records = [poll(s) for s in (*range(0, 100, 20), *range(300, 400, 20))]
    findings, stats = collection_findings(records, 20.0, CFG.collection)
    (gap,) = [f for f in findings if f.category == "collection_gap"]
    assert gap.value == 220.0
    assert "no requests were recorded" in gap.details
    assert (stats.ok_polls, stats.expected_polls) == (10, 20)
    assert stats.coverage == pytest.approx(0.5)


def test_failed_polls_are_an_error_run_and_explain_the_gap() -> None:
    records = [poll(0), poll(20)]
    for s in (40, 60, 80):
        records += [poll(s, ok=False, attempt=a) for a in (1, 2, 3)]
    records += [poll(100), poll(120)]
    findings, stats = collection_findings(records, 20.0, CFG.collection)
    (errors,) = [f for f in findings if f.category == "api_errors"]
    assert errors.value == 3.0
    assert "503 x9" in errors.details
    (gap,) = [f for f in findings if f.category == "collection_gap"]
    assert "failed polls" in gap.details
    assert stats.failed_ticks == 3


def test_identical_responses_in_a_row_are_a_stale_feed() -> None:
    records = [poll(0), poll(20, sha="same"), poll(40, sha="same"), poll(60, sha="same"), poll(80)]
    findings, _ = collection_findings(records, 20.0, CFG.collection)
    (stale,) = [f for f in findings if f.category == "stale_feed"]
    assert stale.value == 3.0
    two = [poll(0), poll(20, sha="same"), poll(40, sha="same"), poll(60)]
    assert not [
        f for f in collection_findings(two, 20.0, CFG.collection)[0] if f.category == "stale_feed"
    ]


def test_runs_finds_consecutive_true_values() -> None:
    mask = np.array([True, True, False, True, False, False, True])
    assert runs(mask) == [(0, 1), (3, 3), (6, 6)]
    assert runs(np.array([], dtype=bool)) == []


def test_distance_to_a_polyline() -> None:
    line = np.array([[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]])
    points = np.array([[50.0, 30.0], [130.0, 40.0], [100.0, 50.0], [-30.0, 40.0]])
    np.testing.assert_allclose(distance_to_polyline(points, line), [30.0, 30.0, 0.0, 50.0])
    np.testing.assert_allclose(
        distance_to_polyline(np.array([[3.0, 4.0]]), np.array([[0.0, 0.0]])), [5.0]
    )


def track(spacing_s: float, offsets_m: list[float], speed_mps: float = 10.0) -> list[Any]:
    """Findings for a vehicle driving straight east, with a sideways error per position."""
    n = len(offsets_m)
    seconds = np.arange(n, dtype=np.float64) * spacing_s
    xy = np.column_stack([seconds * speed_mps, np.array(offsets_m)])
    origin = [T0 + timedelta(seconds=float(x)) for x in seconds]
    return _jump_findings("trip", "line 1", origin, seconds, np.arange(n), xy, 0, CFG.positions)


def test_a_single_far_position_is_a_spike_even_at_a_plausible_speed() -> None:
    # 600 m sideways in 25 s is 24 m/s: below the tram limit, so only the spike rule sees it.
    (finding,) = track(25.0, [0, 0, 600, 0, 0])
    assert finding.category == "gps_jump"
    assert finding.value == pytest.approx(600, abs=1)
    assert "away from the line between its neighbours" in finding.details


def test_a_spike_needs_neighbours_that_are_close_in_time() -> None:
    # The same offset with 100 s between reports: the line between the neighbours is 200 s long.
    assert track(100.0, [0, 0, 600, 0, 0]) == []


def test_gps_noise_is_not_a_jump() -> None:
    assert track(25.0, [0, 6, -5, 8, -7, 4, 0]) == []


def test_an_impossible_speed_is_a_jump_and_overlapping_hits_become_one_finding() -> None:
    findings = track(20.0, [0, 0, 2000, 0, 0])  # 100 m/s there and back, and a spike
    assert len(findings) == 1
    assert findings[0].value == pytest.approx(2000, abs=20)  # plus the 200 m driven


def test_a_level_shift_is_found_by_speed_only_once() -> None:
    findings = track(20.0, [0, 0, 1500, 1500, 1500])
    assert len(findings) == 1
    assert "m/s" in findings[0].details


def test_times_after_midnight_say_so() -> None:
    tz = ZoneInfo("Europe/Prague")
    start = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)  # 22:00 local
    assert _clock(start, tz) == "22:00:00"
    assert _clock(start + timedelta(hours=7), tz, start) == "05:00:00 (+1 day)"
    assert _clock(start + timedelta(hours=1), tz, start) == "23:00:00"


# --- configuration --------------------------------------------------------------------------


def test_jump_speed_depends_on_the_vehicle_type() -> None:
    assert CFG.positions.jump_speed(0) < CFG.positions.jump_speed(2)
    assert CFG.positions.jump_speed(99) == CFG.positions.jump_speed_default_mps


def test_every_category_needs_a_valid_severity() -> None:
    data = CFG.model_dump()
    data["severity"]["gps_jump"] = "catastrophic"
    with pytest.raises(ValidationError):
        AuditConfig.model_validate(data)
    del data["severity"]["gps_jump"]
    with pytest.raises(ValidationError):
        AuditConfig.model_validate(data)


def test_render_markdown_handles_a_day_without_data() -> None:
    summary = {
        "service_date": "2026-01-01",
        "synthetic": False,
        "collection": {
            "first": None,
            "last": None,
            "ok_polls": 0,
            "expected_polls": 0,
            "coverage": None,
            "interval_s": 20.0,
        },
        "positions": {
            "positions": 0,
            "fresh_share": None,
            "on_shape_share": None,
        },
        "findings": {c: {"count": 0, "severity": "info"} for c in CATEGORIES},
        "findings_total": 0,
        "thresholds": json.loads(CFG.model_dump_json()),
        "notes": ["No positions were ingested for this date."],
    }
    text = render_markdown(summary, [], "Europe/Prague")
    assert "SYNTHETIC" not in text
    assert "n/a" in text
    assert "No findings" in text


# --- CLI ------------------------------------------------------------------------------------


def test_cli_audits_one_date_and_all_dates(
    planted: tuple[Path, AuditInputs], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "copy"
    shutil.copytree(planted[0], root, ignore=shutil.ignore_patterns("data"))
    monkeypatch.chdir(REPO)
    runner.invoke(app, ["ingest", "--all", "--archive", str(root)])
    one = runner.invoke(app, ["audit", "--date", "2026-09-28", "--archive", str(root)])
    assert one.exit_code == 0, one.output
    assert "SYNTHETIC 2026-09-28:" in one.output
    assert "findings" in one.output
    every = runner.invoke(app, ["audit", "--all", "--archive", str(root)])
    assert every.exit_code == 0, every.output
    assert every.output.count("SYNTHETIC 2026-") == 1


@pytest.mark.parametrize("args", [[], ["--date", "2026-09-28", "--all"], ["--date", "tomorrow"]])
def test_cli_rejects_bad_arguments(
    args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)
    assert runner.invoke(app, ["audit", "--archive", str(tmp_path), *args]).exit_code == 2


def test_cli_reports_an_archive_without_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)
    result = runner.invoke(app, ["audit", "--all", "--archive", str(tmp_path)])
    assert result.exit_code == 1
    assert "no manifest records" in result.output


def test_positions_frame_has_what_the_audit_reads(planted: tuple[Path, AuditInputs]) -> None:
    _, inputs = planted
    frame = pl.read_parquet(inputs.ingest.positions_dir / f"service_date={DAY}" / "part-0.parquet")
    assert {"shape_dist_km", "handover_duplicate", "route_short_name", "fetched_at"} <= set(
        frame.columns
    )
