"""Offline tests for GTFS snapshots, the version registry and the loader."""

import io
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import polars as pl
import pytest
import respx
from typer.testing import CliRunner

from headway.cli import app
from headway.gtfs import (
    GtfsIntegrityError,
    GtfsVersionNotFoundError,
    ResolvedGtfs,
    gtfs_time_to_utc,
    load_feed,
    read_registry,
    rebuild_registry,
    resolve_gtfs,
    snapshot_gtfs,
)
from headway.gtfs.registry import feed_date_range

REPO = Path(__file__).resolve().parents[1]
FEED_DIR = REPO / "tests" / "fixtures" / "gtfs" / "synthetic"
URL = "https://gtfs.example.invalid/PID_GTFS.zip"


def make_zip(drop: tuple[str, ...] = (), extra: dict[str, str] | None = None) -> bytes:
    """Zip the synthetic feed deterministically (fixed timestamps, sorted members)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        files = {p.name: p.read_text(encoding="utf-8") for p in sorted(FEED_DIR.glob("*.txt"))}
        files.update(extra or {})
        for name in sorted(files):
            if name not in drop:
                archive.writestr(zipfile.ZipInfo(name, (2026, 10, 20, 0, 0, 0)), files[name])
    return buffer.getvalue()


def take_snapshot(gtfs_dir: Path, body: bytes, day: date) -> bool:
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(200, content=body))
        now = datetime(day.year, day.month, day.day, 3, 0, tzinfo=UTC)
        return snapshot_gtfs(URL, gtfs_dir, timeout_s=5, now=now).stored


def test_snapshot_stores_and_registers(tmp_path: Path) -> None:
    assert take_snapshot(tmp_path, make_zip(), date(2026, 10, 20))
    (version,) = read_registry(tmp_path)
    assert version.path.startswith("PID_GTFS_2026-10-20_")
    assert version.path == f"PID_GTFS_2026-10-20_{version.sha256[:12]}.zip"
    assert (version.feed_start_date, version.feed_end_date) == (
        date(2026, 10, 20),
        date(2026, 11, 2),
    )
    assert (tmp_path / version.path).read_bytes() == make_zip()


def test_unchanged_feed_is_not_stored_again(tmp_path: Path) -> None:
    assert take_snapshot(tmp_path, make_zip(), date(2026, 10, 20))
    assert not take_snapshot(tmp_path, make_zip(), date(2026, 10, 21))
    assert len(list(tmp_path.glob("*.zip"))) == 1
    assert len(read_registry(tmp_path)) == 1


def test_changed_feed_creates_a_new_version(tmp_path: Path) -> None:
    take_snapshot(tmp_path, make_zip(), date(2026, 10, 20))
    changed = make_zip(
        extra={
            "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
            "SYN,Renamed,https://example.invalid,Europe/Prague\n"
        }
    )
    assert take_snapshot(tmp_path, changed, date(2026, 10, 21))
    assert [v.snapshot_date for v in read_registry(tmp_path)] == [
        date(2026, 10, 20),
        date(2026, 10, 21),
    ]


def test_failed_download_stores_nothing(tmp_path: Path) -> None:
    with respx.mock:
        respx.get(URL).mock(return_value=httpx.Response(503))
        with pytest.raises(httpx.HTTPStatusError):
            snapshot_gtfs(URL, tmp_path, timeout_s=5)
    assert list(tmp_path.iterdir()) == []


def test_date_range_falls_back_to_calendar(tmp_path: Path) -> None:
    path = tmp_path / "feed.zip"
    path.write_bytes(
        make_zip(
            drop=("feed_info.txt",),
            extra={"calendar_dates.txt": "service_id,date,exception_type\nWD,20261105,1\n"},
        )
    )
    assert feed_date_range(path) == (date(2026, 10, 20), date(2026, 11, 5))


def test_rebuild_registry_and_name_hash_check(tmp_path: Path) -> None:
    take_snapshot(tmp_path, make_zip(), date(2026, 10, 20))
    (tmp_path / "gtfs_versions.parquet").unlink()
    assert len(rebuild_registry(tmp_path)) == 1

    (tmp_path / "PID_GTFS_2026-10-21_000000000000.zip").write_bytes(make_zip())
    with pytest.raises(ValueError, match="does not match name"):
        rebuild_registry(tmp_path)


@pytest.fixture
def two_versions(tmp_path: Path) -> Path:
    take_snapshot(tmp_path, make_zip(), date(2026, 10, 20))
    later = make_zip(
        extra={
            "feed_info.txt": "feed_publisher_name,feed_publisher_url,feed_lang,"
            "feed_start_date,feed_end_date\nHeadway tests,https://example.invalid,"
            "cs,20261024,20261106\n"
        }
    )
    take_snapshot(tmp_path, later, date(2026, 10, 24))
    return tmp_path


def test_resolve_picks_newest_snapshot_on_or_before_the_date(two_versions: Path) -> None:
    assert resolve_gtfs(date(2026, 10, 23), two_versions).version.snapshot_date == date(
        2026, 10, 20
    )
    assert resolve_gtfs(date(2026, 10, 24), two_versions).version.snapshot_date == date(
        2026, 10, 24
    )
    assert resolve_gtfs(date(2026, 11, 5), two_versions).version.snapshot_date == date(2026, 10, 24)


@pytest.mark.parametrize("day", [date(2026, 10, 19), date(2026, 11, 7)])
def test_resolve_raises_when_nothing_covers_the_date(two_versions: Path, day: date) -> None:
    with pytest.raises(GtfsVersionNotFoundError):
        resolve_gtfs(day, two_versions)


def test_schedule_is_unreachable_without_resolve(two_versions: Path) -> None:
    version = read_registry(two_versions)[0]
    with pytest.raises(TypeError, match="resolve_gtfs"):
        ResolvedGtfs(date(2026, 10, 21), version, two_versions)
    with pytest.raises(TypeError, match="resolve_gtfs"):
        load_feed(two_versions / version.path)  # type: ignore[arg-type]


def test_loader_types(two_versions: Path) -> None:
    feed = load_feed(resolve_gtfs(date(2026, 10, 21), two_versions))
    stop_times, stops, calendar = feed["stop_times"], feed["stops"], feed["calendar"]
    assert stop_times.schema["stop_sequence"] == pl.Int32
    assert stop_times.schema["shape_dist_traveled"] == pl.Float64
    assert stop_times.schema["arrival_time"] == pl.String
    assert stops.schema["stop_lat"] == pl.Float64
    assert stops.schema["wheelchair_boarding"] == pl.Int8
    assert stops.filter(pl.col("stop_id") == "S5")["wheelchair_boarding"].item() is None
    assert calendar.schema["start_date"] == pl.Date
    assert calendar["start_date"].min() == date(2026, 10, 20)
    assert feed["calendar_dates"].schema["date"] == pl.Date
    assert set(feed.tables) == {
        "routes",
        "trips",
        "stop_times",
        "stops",
        "shapes",
        "calendar",
        "calendar_dates",
    }


def test_integrity_check_detects_a_modified_snapshot(two_versions: Path) -> None:
    resolved = resolve_gtfs(date(2026, 10, 21), two_versions)
    resolved.zip_path.write_bytes(make_zip(drop=("shapes.txt",)))
    with pytest.raises(GtfsIntegrityError):
        load_feed(resolved)


def test_trip_past_midnight_converts_through_the_resolved_feed(two_versions: Path) -> None:
    day = date(2026, 10, 24)  # Saturday: service WE
    feed = load_feed(resolve_gtfs(day, two_versions), tables=("stop_times",))
    last = feed["stop_times"].filter(pl.col("trip_id") == "T1_WE_2450").sort("stop_sequence")[-1]
    assert last["arrival_time"].item() == "25:13:00"
    utc = gtfs_time_to_utc(feed.resolved.service_date, last["arrival_time"].item())
    assert utc == datetime(2026, 10, 24, 23, 13, tzinfo=UTC)


@respx.mock
def test_cli_snapshot_gtfs(tmp_path: Path) -> None:
    respx.get("https://data.pid.cz/PID_GTFS.zip").mock(
        return_value=httpx.Response(200, content=make_zip())
    )
    env = {"HEADWAY_PATHS__GTFS_DIR": str(tmp_path)}
    config = str(REPO / "config" / "default.toml")
    first = CliRunner().invoke(app, ["snapshot-gtfs", "--config", config], env=env)
    assert first.exit_code == 0, first.output
    assert "covers 2026-10-20 to 2026-11-02" in first.output
    second = CliRunner().invoke(app, ["snapshot-gtfs", "--config", config], env=env)
    assert "unchanged" in second.output
    rebuilt = CliRunner().invoke(
        app, ["snapshot-gtfs", "--rebuild-registry", "--config", config], env=env
    )
    assert "1 snapshot(s)" in rebuilt.output
