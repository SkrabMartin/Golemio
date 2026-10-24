"""Offline tests for the collector (layer L0). All HTTP is mocked with respx."""

import asyncio
import gzip
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest
import respx
from pydantic import SecretStr
from typer.testing import CliRunner

from headway.cli import app
from headway.collect import Collector, ManifestWriter, PollRecord, read_manifest
from headway.collect.health import HealthMonitor
from headway.collect.manifest import MANIFEST_SCHEMA, manifest_path
from headway.collect.poller import AUTH_HEADER, next_tick, parse_retry_after
from headway.collect.storage import remove_stale_tmp, write_atomic
from headway.config import CollectConfig, load_settings

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "default.toml"
FIXTURE = REPO / "tests" / "fixtures" / "golemio"
BODY = (FIXTURE / "vehiclepositions_synthetic_20261024T104828Z.json").read_bytes()
BODY_2 = (FIXTURE / "vehiclepositions_synthetic_20261024T104858Z.json").read_bytes()
TOKEN = "dummy-token-for-tests"
START = datetime(2026, 10, 24, 10, 48, 20, tzinfo=UTC)


class FakeTime:
    """A clock that only moves when the collector sleeps."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def _cfg(**overrides: object) -> CollectConfig:
    return load_settings(CONFIG).collect.model_copy(update=overrides)


def _collector(tmp_path: Path, fake: FakeTime | None = None, **overrides: object) -> Collector:
    fake = fake or FakeTime()
    return Collector(
        cfg=_cfg(**overrides),
        raw_dir=tmp_path / "raw",
        manifest=ManifestWriter(tmp_path / "manifest"),
        token=SecretStr(TOKEN),
        clock=fake.clock,
        sleep=fake.sleep,
    )


def _rows(tmp_path: Path) -> list[PollRecord]:
    return read_manifest(manifest_path(tmp_path / "manifest", START.date()))


@respx.mock
def test_success_stores_body_unmodified_and_records_manifest(tmp_path: Path) -> None:
    route = respx.get(_cfg().url).mock(return_value=httpx.Response(200, content=BODY))
    record = asyncio.run(_collector(tmp_path).once())

    assert record.ok
    request = route.calls.last.request
    assert request.headers[AUTH_HEADER] == TOKEN
    assert request.url.params["limit"] == "10000"
    stored = tmp_path / "raw" / str(record.path)
    assert stored.name.startswith("104820_")
    assert stored.name.endswith(".json.gz")
    assert gzip.decompress(stored.read_bytes()) == BODY

    (row,) = _rows(tmp_path)
    assert row == record
    assert row.bytes == len(BODY)
    assert row.stored_bytes == stored.stat().st_size


@respx.mock
def test_token_never_written_to_disk(tmp_path: Path) -> None:
    respx.get(_cfg().url).mock(return_value=httpx.Response(200, content=BODY))
    asyncio.run(_collector(tmp_path).once())
    for path in tmp_path.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert TOKEN.encode() not in data
            if path.suffix == ".gz":
                assert TOKEN.encode() not in gzip.decompress(data)


def test_api_key_is_not_a_default_header(tmp_path: Path) -> None:
    # The same client is used for ntfy notifications, which must never see the key.
    client = _collector(tmp_path).client()
    assert AUTH_HEADER not in client.headers
    asyncio.run(client.aclose())


@respx.mock
def test_429_with_retry_after_is_honoured(tmp_path: Path) -> None:
    respx.get(_cfg().url).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}, content=b"slow down"),
            httpx.Response(200, content=BODY),
        ]
    )
    fake = FakeTime()
    record = asyncio.run(_collector(tmp_path, fake).once())

    assert record.ok
    assert record.attempt == 2
    assert fake.sleeps == [3.0]  # Retry-After wins over the 1 s initial backoff
    first, second = _rows(tmp_path)
    assert (first.attempt, first.http_status, first.error) == (1, 429, "HTTP 429")
    assert (second.attempt, second.http_status) == (2, 200)


@respx.mock
def test_retry_after_beyond_the_tick_defers_later_ticks(tmp_path: Path) -> None:
    respx.get(_cfg().url).mock(return_value=httpx.Response(429, headers={"Retry-After": "120"}))
    fake = FakeTime()
    collector = _collector(tmp_path, fake)
    record = asyncio.run(collector.once())

    assert not record.ok
    assert record.http_status == 429
    assert fake.sleeps == []  # waiting 120 s would overrun the 20 s tick
    assert collector._not_before == START + timedelta(seconds=120)


@respx.mock
def test_timeouts_are_retried_with_backoff_and_recorded(tmp_path: Path) -> None:
    respx.get(_cfg().url).mock(side_effect=httpx.ConnectTimeout("timed out"))
    fake = FakeTime()
    record = asyncio.run(_collector(tmp_path, fake).once())

    assert not record.ok
    assert record.attempt == 3
    assert record.error is not None
    assert record.error.startswith("ConnectTimeout")
    assert fake.sleeps == [1.0, 2.0]
    rows = _rows(tmp_path)
    assert [r.attempt for r in rows] == [1, 2, 3]
    assert all(r.http_status is None and r.path is None for r in rows)
    assert not list((tmp_path / "raw").rglob("*.gz"))


@respx.mock
def test_identical_consecutive_bodies_are_flagged_stale(tmp_path: Path) -> None:
    respx.get(_cfg().url).mock(return_value=httpx.Response(200, content=BODY))
    fake = FakeTime()
    collector = _collector(tmp_path, fake)

    async def three_polls() -> list[str]:
        kinds = []
        async with collector.client() as client:
            for _ in range(3):
                final = await collector.tick(client, fake.now + timedelta(seconds=20))
                kinds += [e.kind for e in collector.health.observe(final)]
                fake.now += timedelta(seconds=20)
        return kinds

    assert asyncio.run(three_polls()) == ["stale_feed"]
    assert collector.health.identical_count == 3


def test_health_reports_error_streak_gap_and_recovery() -> None:
    monitor = HealthMonitor(
        interval_s=20, stale_after_identical=3, error_streak_alert=2, gap_factor=2
    )

    def ok(at: datetime, sha: str) -> PollRecord:
        return PollRecord(fetched_at=at, attempt=1, http_status=200, sha256=sha, path="p")

    def failed(at: datetime) -> PollRecord:
        return PollRecord(fetched_at=at, attempt=3, error="ConnectTimeout")

    t = START
    assert monitor.observe(ok(t, "a")) == []
    assert monitor.observe(failed(t + timedelta(seconds=20))) == []
    assert [e.kind for e in monitor.observe(failed(t + timedelta(seconds=40)))] == ["error_streak"]
    events = monitor.observe(ok(t + timedelta(seconds=60), "b"))
    assert [e.kind for e in events] == ["errors_recovered", "gap"]
    assert monitor.total_ok == 2
    assert monitor.total_failed == 2


def test_crash_mid_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "raw" / "2026" / "10" / "24" / "104820_abc.json.gz"

    def broken_fsync(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(OSError, match="disk full"):
        write_atomic(target, b"partial")
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


def test_stale_temporary_files_are_removed(tmp_path: Path) -> None:
    leftover = tmp_path / "2026" / "10" / "24" / "104820_abc.json.gz.123.tmp"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"half")
    assert remove_stale_tmp(tmp_path) == [leftover]
    assert not leftover.exists()


def test_manifest_round_trip_appends_and_splits_by_utc_day(tmp_path: Path) -> None:
    writer = ManifestWriter(tmp_path)
    late = datetime(2026, 10, 24, 23, 59, 50, tzinfo=UTC)
    first = PollRecord(
        fetched_at=late,
        attempt=1,
        http_status=200,
        sha256="a" * 64,
        bytes=10,
        stored_bytes=5,
        latency_ms=12.5,
        path="2026/10/24/235950_aaaa.json.gz",
    )
    writer.add(first)
    writer.flush()
    second = PollRecord(
        fetched_at=late + timedelta(seconds=20), attempt=2, error="HTTP 503", http_status=503
    )
    third = PollRecord(fetched_at=late - timedelta(seconds=20), attempt=1, http_status=200)
    writer.add(second)
    writer.add(third)
    writer.flush()

    day1 = manifest_path(tmp_path, late.date())
    day2 = manifest_path(tmp_path, (late + timedelta(seconds=20)).date())
    assert read_manifest(day1) == [first, third]
    assert read_manifest(day2) == [second]
    assert pq.read_schema(day1).equals(MANIFEST_SCHEMA)
    assert writer.pending == 0


def test_next_tick_is_aligned_to_the_wall_clock() -> None:
    assert next_tick(datetime(2026, 10, 24, 10, 48, 21, tzinfo=UTC), 20) == datetime(
        2026, 10, 24, 10, 48, 40, tzinfo=UTC
    )
    assert next_tick(datetime(2026, 10, 24, 10, 48, 40, tzinfo=UTC), 20) == datetime(
        2026, 10, 24, 10, 49, 0, tzinfo=UTC
    )


def test_parse_retry_after() -> None:
    assert parse_retry_after("7", START) == 7.0
    assert parse_retry_after("Sat, 24 Oct 2026 10:48:50 GMT", START) == 30.0
    assert parse_retry_after("soon", START) is None
    assert parse_retry_after(None, START) is None


@respx.mock
def test_run_loop_stops_gracefully_and_flushes(tmp_path: Path) -> None:
    respx.get(_cfg().url).mock(
        side_effect=[httpx.Response(200, content=BODY)] + [httpx.Response(200, content=BODY_2)] * 10
    )
    collector = Collector(
        cfg=_cfg(interval_s=1),
        raw_dir=tmp_path / "raw",
        manifest=ManifestWriter(tmp_path / "manifest"),
        token=SecretStr(TOKEN),
    )

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(collector.run(stop))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if collector.health.total_ok >= 1:
                break
        stop.set()  # what the SIGTERM handler does
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    rows = [r for f in (tmp_path / "manifest").glob("*.parquet") for r in read_manifest(f)]
    assert len(rows) >= 1
    assert all(r.ok for r in rows)
    assert len(list((tmp_path / "raw").rglob("*.json.gz"))) == len(rows)


def _cli_env(tmp_path: Path, token: str | None) -> dict[str, str | None]:
    return {
        "GOLEMIO_TOKEN": token,
        "HEADWAY_PATHS__RAW_DIR": str(tmp_path / "raw"),
        "HEADWAY_PATHS__MANIFEST_DIR": str(tmp_path / "manifest"),
    }


@respx.mock
def test_cli_collect_once_against_mocked_server(tmp_path: Path) -> None:
    respx.get(_cfg().url).mock(return_value=httpx.Response(200, content=BODY))
    result = CliRunner().invoke(
        app, ["collect", "--once", "--config", str(CONFIG)], env=_cli_env(tmp_path, TOKEN)
    )
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "raw").rglob("*.json.gz"))) == 1
    assert len(list((tmp_path / "manifest").glob("*.parquet"))) == 1
    assert TOKEN not in result.output


def test_cli_collect_without_token_fails(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["collect", "--once", "--config", str(CONFIG)], env=_cli_env(tmp_path, None)
    )
    assert result.exit_code == 1
    assert "GOLEMIO_TOKEN is not set" in result.output
