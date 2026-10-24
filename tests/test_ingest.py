"""Tests for layer L1 (ingest): parser, service dates, accounting, replay and the CLI."""

import dataclasses
import gzip
import hashlib
import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from headway.cli import app
from headway.collect import PollRecord, read_manifest
from headway.collect.manifest import ManifestWriter
from headway.collect.storage import compress, raw_relpath
from headway.config import load_settings
from headway.gtfs import resolve_gtfs
from headway.ingest.parser import parse_poll, vehicle_key
from headway.ingest.pipeline import (
    IngestInputs,
    available_service_dates,
    ingest_dates,
    inputs_for,
    service_date_of,
)
from headway.synth import SynthConfig, generate, load_synth_config

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "golemio"
SETTINGS = load_settings(REPO / "config" / "default.toml")
PRAGUE = ZoneInfo("Europe/Prague")
TZ = "Europe/Prague"
DAY = date(2026, 9, 28)
# Monday 2026-09-28, 07:00-08:00 local (morning peak).
MORNING = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)
# 03:30-05:30 local on 2026-09-29: night trips of the 28th, then the first trips of the 29th.
BOUNDARY = datetime(2026, 9, 29, 1, 30, tzinfo=UTC)

runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})

Key = tuple[str, datetime, datetime]


def synth_cfg() -> SynthConfig:
    """A small network with frequent defects, so one hour holds every kind."""
    base = load_synth_config(REPO / "config" / "synth.toml").model_dump()
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
    return SynthConfig.model_validate(base)


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("ingest") / "archive"
    generate(synth_cfg(), out, start_at=MORNING, hours=1.0)
    return out


@pytest.fixture(scope="module")
def boundary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("boundary") / "archive"
    generate(synth_cfg(), out, start_at=BOUNDARY, hours=2.0)
    return out


def run_ingest(root: Path, days: list[date]) -> tuple[IngestInputs, list[dict[str, Any]]]:
    inputs = inputs_for(root, SETTINGS)
    return inputs, ingest_dates(days, inputs)


def positions(inputs: IngestInputs, day: date) -> list[dict[str, Any]]:
    path = inputs.positions_dir / f"service_date={day}" / "part-0.parquet"
    rows: list[dict[str, Any]] = pq.read_table(path).to_pylist()
    return rows


def without_benchmark(report: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in report.items() if k != "benchmark"}


def manifest_rows(root: Path) -> list[PollRecord]:
    rows = [
        r for f in sorted((root / "raw" / "manifest").glob("*.parquet")) for r in read_manifest(f)
    ]
    return sorted(rows, key=lambda r: (r.fetched_at, r.attempt))


def observations(root: Path) -> dict[Key, tuple[datetime, str, int]]:
    """Independent pass over every poll: first fetch, first file and count per observation."""
    seen: dict[Key, tuple[datetime, str, int]] = {}
    for record in manifest_rows(root):
        if not record.ok:
            continue
        body = gzip.decompress((root / "raw" / str(record.path)).read_bytes())
        for row in parse_poll(body).rows:
            key = (row["trip_id"], row["trip_start_ts"], row["origin_ts"])
            first = seen.get(key)
            seen[key] = (
                (record.fetched_at, str(record.path), 1)
                if first is None
                else (first[0], first[1], first[2] + 1)
            )
    return seen


def service_day(ts: datetime) -> date:
    """The service day, written out independently of the code under test."""
    return (ts.astimezone(PRAGUE) - timedelta(minutes=SETTINGS.ingest.service_day_start_min)).date()


def fixture_body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


GOOD = "vehiclepositions_synthetic_20261024T104828Z.json"
SECOND = "vehiclepositions_synthetic_20261024T104858Z.json"


# --- parser -----------------------------------------------------------------------------


def test_vehicle_key_includes_the_route_type() -> None:
    assert vehicle_key(0, 1234) == "0-1234"
    assert vehicle_key(3, 1234) == "3-1234"
    assert vehicle_key(3, None) is None


def test_every_feature_becomes_a_row_in_utc() -> None:
    body = fixture_body(GOOD)
    parsed = parse_poll(body, check_schema=True)
    features = json.loads(body)["features"]
    assert parsed.feature_count == len(features) == len(parsed.rows)
    assert parsed.rejects == []
    assert parsed.extra_fields == set()
    first = parsed.rows[0]
    trip = features[0]["properties"]["trip"]
    assert first["trip_id"] == trip["gtfs"]["trip_id"]
    assert (
        first["vehicle_id"] == f"{trip['gtfs']['route_type']}-{trip['vehicle_registration_number']}"
    )
    assert first["origin_ts"].utcoffset() == timedelta(0)
    assert [first["lon"], first["lat"]] == features[0]["geometry"]["coordinates"]


def test_an_empty_feed_is_valid() -> None:
    parsed = parse_poll(fixture_body("vehiclepositions_empty.json"))
    assert (parsed.rows, parsed.rejects, parsed.feature_count) == ([], [], 0)


def test_one_invalid_feature_is_rejected_alone() -> None:
    data = json.loads(fixture_body(GOOD))
    data["features"][1]["properties"]["last_position"]["state_position"] = "teleporting"
    parsed = parse_poll(json.dumps(data).encode())
    assert [(r.feature_index, r.reason) for r in parsed.rejects] == [(1, "invalid_feature")]
    assert "state_position" in parsed.rejects[0].detail
    assert len(parsed.rows) == len(data["features"]) - 1
    assert parsed.feature_count == len(data["features"])


def test_the_malformed_fixture_yields_no_rows_but_a_reject() -> None:
    parsed = parse_poll(fixture_body("vehiclepositions_malformed.json"))
    assert parsed.rows == []
    assert [r.reason for r in parsed.rejects] == ["invalid_feature"]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"not json", "invalid_json"),
        (b"[]", "invalid_feed"),
        (b'{"type": "FeatureCollection", "features": "x"}', "invalid_feed"),
        (b'{"type": "Something", "features": []}', "invalid_feed"),
    ],
)
def test_an_unusable_body_is_rejected_whole(body: bytes, reason: str) -> None:
    parsed = parse_poll(body)
    assert parsed.rows == []
    assert [(r.feature_index, r.reason) for r in parsed.rejects] == [(None, reason)]


# --- service dates ----------------------------------------------------------------------


def test_a_service_day_starts_at_0430_local_time() -> None:
    def at(year: int, month: int, day: int, hour: int, minute: int) -> date:
        return service_date_of(datetime(year, month, day, hour, minute, tzinfo=UTC), TZ, 270)

    assert at(2026, 9, 28, 2, 29) == date(2026, 9, 27)  # 04:29 CEST
    assert at(2026, 9, 28, 2, 30) == date(2026, 9, 28)  # 04:30 CEST
    assert at(2026, 9, 28, 22, 30) == date(2026, 9, 28)  # 00:30 on the 29th: a night trip
    # Summer time ends on 2026-10-25; the boundary is 04:30 local on both sides of it.
    assert at(2026, 10, 25, 3, 29) == date(2026, 10, 24)  # 04:29 CET
    assert at(2026, 10, 25, 3, 30) == date(2026, 10, 25)  # 04:30 CET


# --- the pipeline on a synthetic archive --------------------------------------------------


def test_every_observation_is_written_once_with_the_first_provenance(archive: Path) -> None:
    inputs, (report,) = run_ingest(archive, [DAY])
    expected = observations(archive)
    rows = positions(inputs, DAY)
    assert {(r["trip_id"], r["trip_start_ts"], r["origin_ts"]) for r in rows} == set(expected)
    assert len(rows) == len(expected) == report["positions"]
    for row in rows:
        first_fetch, first_path, count = expected[
            (row["trip_id"], row["trip_start_ts"], row["origin_ts"])
        ]
        assert (row["fetched_at"], row["raw_path"], row["seen_in_polls"]) == (
            first_fetch,
            first_path,
            count,
        )
    repeats = sum(count - 1 for _, _, count in expected.values())
    assert report["repeated_observations_dropped"] == repeats > 0
    assert report["repeats_with_changed_content"] == 0
    assert report["unknown_fields"] == []
    assert report["rejects"] == 0


def test_poll_counts_follow_the_manifest(archive: Path) -> None:
    _, (report,) = run_ingest(archive, [DAY])
    records = manifest_rows(archive)
    assert report["owned_polls_ok"] == sum(r.ok for r in records)
    not_ok = [r for r in records if not r.ok]
    assert not_ok
    assert report["owned_polls_not_ok_by_status"] == {
        "503": sum(r.http_status == 503 for r in not_ok)
    }
    assert report["owned_polls_at_limit"] == 0


def test_rows_carry_the_service_date_and_the_gtfs_version(archive: Path) -> None:
    inputs, _ = run_ingest(archive, [DAY])
    version = resolve_gtfs(DAY, inputs.gtfs_dir).version
    rows = positions(inputs, DAY)
    assert {r["service_date"] for r in rows} == {DAY}
    assert {r["gtfs_sha256"] for r in rows} == {version.sha256}
    assert {r["gtfs_snapshot_date"] for r in rows} == {version.snapshot_date}
    assert {r["parser_version"] for r in rows} == {"v1"}
    assert sorted(p.name for p in inputs.positions_dir.iterdir()) == [f"service_date={DAY}"]


def test_ingest_is_idempotent_and_replaces_the_partition(archive: Path) -> None:
    inputs, (first,) = run_ingest(archive, [DAY])
    folder = inputs.positions_dir / f"service_date={DAY}"
    before = pq.read_table(folder / "part-0.parquet")
    (folder / "part-7.parquet").write_bytes(b"stale leftovers")
    _, (second,) = run_ingest(archive, [DAY])
    assert pq.read_table(folder / "part-0.parquet").equals(before)
    assert [p.name for p in folder.iterdir()] == ["part-0.parquet"]
    assert without_benchmark(first) == without_benchmark(second)


def test_handover_duplicates_are_flagged_not_dropped(archive: Path) -> None:
    inputs, (report,) = run_ingest(archive, [DAY])
    rows = positions(inputs, DAY)
    planted = [
        d
        for d in pq.read_table(archive / "truth" / "defects.parquet").to_pylist()
        if d["kind"] == "handover_duplicate"
    ]
    assert planted
    by_message: dict[tuple[Any, ...], set[str]] = {}
    for r in rows:
        by_message.setdefault((r["vehicle_id"], r["origin_ts"], r["lat"], r["lon"]), set()).add(
            r["trip_id"]
        )
    for r in rows:
        shared = len(by_message[(r["vehicle_id"], r["origin_ts"], r["lat"], r["lon"])]) > 1
        assert r["handover_duplicate"] == (r["vehicle_id"] is not None and shared)
    flagged = {r["trip_id"] for r in rows if r["handover_duplicate"]}
    carriers = {d["trip_id"] for d in planted}
    assert flagged & carriers
    assert report["handover_duplicate_rows"] == sum(r["handover_duplicate"] for r in rows)


def test_origin_marks_the_data_as_synthetic(archive: Path) -> None:
    inputs, (report,) = run_ingest(archive, [DAY])
    origin = json.loads((inputs.positions_dir.parent / "ORIGIN.json").read_text(encoding="utf-8"))
    assert origin["synthetic"] is True
    assert report["synthetic"] is True


def test_observations_are_split_at_the_service_day_boundary(boundary: Path) -> None:
    days = [date(2026, 9, 28), date(2026, 9, 29)]
    inputs, reports = run_ingest(boundary, days)
    expected = observations(boundary)
    for day in days:
        want = {key for key in expected if service_day(key[1]) == day}
        rows = positions(inputs, day)
        assert want, day
        assert {(r["trip_id"], r["trip_start_ts"], r["origin_ts"]) for r in rows} == want
    assert sum(r["positions"] for r in reports) == len(expected)
    assert available_service_dates(inputs) == days


# --- rejects, limits and missing timetables -----------------------------------------------


def write_poll(
    root: Path,
    writer: ManifestWriter,
    fetched_at: datetime,
    body: bytes,
    *,
    manifest_sha: str | None = None,
    file_bytes: bytes | None = None,
    write_file: bool = True,
) -> str:
    sha = hashlib.sha256(body).hexdigest()
    rel = raw_relpath(fetched_at, sha)
    if write_file:
        path = root / "raw" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(compress(body) if file_bytes is None else file_bytes)
    writer.add(
        PollRecord(
            fetched_at=fetched_at,
            attempt=1,
            http_status=200,
            sha256=manifest_sha or sha,
            bytes=len(body),
            path=rel.as_posix(),
        )
    )
    return sha


@pytest.fixture
def damaged(tmp_path: Path) -> tuple[Path, str]:
    """A hand-made, non-synthetic archive with one poll of every kind of problem."""
    root = tmp_path / "damaged"
    writer = ManifestWriter(root / "raw" / "manifest")
    t0 = datetime(2026, 10, 24, 10, 48, 0, tzinfo=UTC)

    def at(seconds: int) -> datetime:
        return t0 + timedelta(seconds=seconds)

    good = fixture_body(GOOD)
    sha = write_poll(root, writer, at(0), good)
    broken = json.loads(fixture_body(SECOND))
    broken["features"][3]["properties"]["last_position"]["state_position"] = "teleporting"
    write_poll(root, writer, at(30), json.dumps(broken).encode())
    write_poll(root, writer, at(60), b"not json")
    writer.add(PollRecord(fetched_at=at(90), attempt=1, http_status=503, error="HTTP 503"))
    write_poll(
        root, writer, at(120), b'{"type":"FeatureCollection","features":[]}', write_file=False
    )
    write_poll(
        root, writer, at(150), b'{"type":"FeatureCollection","features":[1]}', manifest_sha="0" * 64
    )
    write_poll(root, writer, at(180), b'{"x": 1}', file_bytes=b"this is not gzip")
    writer.flush()
    return root, sha


def test_every_problem_is_rejected_once_with_a_reason(damaged: tuple[Path, str]) -> None:
    root, _ = damaged
    inputs, (report,) = run_ingest(root, [date(2026, 10, 24)])
    assert report["owned_polls_ok"] == 2
    assert report["owned_polls_rejected"] == 4
    assert report["owned_polls_not_ok_by_status"] == {"503": 1}
    assert report["reject_reasons"] == {
        "invalid_feature": 1,
        "invalid_json": 1,
        "raw_file_missing": 1,
        "sha256_mismatch": 1,
        "unreadable_gzip": 1,
    }
    rejects = pq.read_table(
        inputs.rejects_dir / "service_date=2026-10-24" / "part-0.parquet"
    ).to_pylist()
    assert len(rejects) == report["rejects"] == 5
    assert [r["feature_index"] for r in rejects if r["reason"] == "invalid_feature"] == [3]
    assert all(r["detail"] for r in rejects)
    # Nothing but the one bad feature was lost from the readable polls.
    good = parse_poll(fixture_body(GOOD)).rows
    second = parse_poll(fixture_body(SECOND)).rows
    unique = {(r["trip_id"], r["trip_start_ts"], r["origin_ts"]) for r in good + second}
    assert report["positions"] <= len(unique)
    assert report["positions"] >= len(unique) - 1


def test_a_poll_with_as_many_features_as_the_limit_is_flagged(damaged: tuple[Path, str]) -> None:
    root, good_sha = damaged
    inputs = dataclasses.replace(inputs_for(root, SETTINGS), limit=19)
    (report,) = ingest_dates([date(2026, 10, 24)], inputs)
    assert report["owned_polls_at_limit"] == 1
    rows = positions(inputs, date(2026, 10, 24))
    assert {r["poll_at_limit"] for r in rows if r["raw_sha256"] == good_sha} == {True}
    assert {r["poll_at_limit"] for r in rows if r["raw_sha256"] != good_sha} <= {False}


def test_a_missing_timetable_is_reported_not_guessed(damaged: tuple[Path, str]) -> None:
    root, _ = damaged
    inputs, (report,) = run_ingest(root, [date(2026, 10, 24)])
    assert report["gtfs"] is None
    assert report["synthetic"] is False
    assert {r["gtfs_sha256"] for r in positions(inputs, date(2026, 10, 24))} == {None}
    origin = json.loads((inputs.positions_dir.parent / "ORIGIN.json").read_text(encoding="utf-8"))
    assert origin == {"synthetic": False}


# --- CLI --------------------------------------------------------------------------------


def test_cli_ingests_one_date_and_all_dates(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "copy"
    shutil.copytree(archive, root, ignore=shutil.ignore_patterns("data"))
    monkeypatch.chdir(REPO)
    one = runner.invoke(app, ["ingest", "--date", "2026-09-28", "--archive", str(root)])
    assert one.exit_code == 0, one.output
    assert "SYNTHETIC 2026-09-28:" in one.output
    every = runner.invoke(app, ["ingest", "--all", "--archive", str(root)])
    assert every.exit_code == 0, every.output
    assert every.output.count("SYNTHETIC 2026-") == 1


@pytest.mark.parametrize(
    "args",
    [[], ["--date", "2026-09-28", "--all"], ["--date", "28.9.2026"]],
)
def test_cli_rejects_bad_arguments(
    args: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)
    result = runner.invoke(app, ["ingest", "--archive", str(tmp_path), *args])
    assert result.exit_code == 2


def test_cli_reports_an_archive_without_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)
    result = runner.invoke(app, ["ingest", "--all", "--archive", str(tmp_path)])
    assert result.exit_code == 1
    assert "no manifest records" in result.output
