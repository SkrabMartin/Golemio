"""The polling loop of the collector (layer L0).

Every ``interval_s`` seconds, aligned to the wall clock, one *tick* requests the
vehicle-positions endpoint. Failed attempts (transport errors, HTTP 429 and 5xx) are
retried with exponential backoff, honouring ``Retry-After``, but never past the start of the
next tick. Every attempt is recorded in the manifest, and every response body is stored
unmodified (after HTTP content decoding) before anything else happens.
"""

import asyncio
import contextlib
import hashlib
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from pydantic import SecretStr
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type

from headway.collect.health import HealthEvent, HealthMonitor
from headway.collect.manifest import ManifestWriter, PollRecord
from headway.collect.notify import Notifier
from headway.collect.storage import compress, raw_relpath, remove_stale_tmp, write_atomic
from headway.config import CollectConfig

log = logging.getLogger(__name__)

AUTH_HEADER = "X-Access-Token"
USER_AGENT = "headway-collector (+https://github.com/SkrabMartin/headway)"
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def utcnow() -> datetime:
    """Current time in UTC."""
    return datetime.now(UTC)


def next_tick(now: datetime, interval_s: int) -> datetime:
    """Return the first wall-clock multiple of ``interval_s`` strictly after ``now``."""
    ts = now.timestamp()
    return datetime.fromtimestamp((math.floor(ts / interval_s) + 1) * interval_s, UTC)


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Parse a ``Retry-After`` header (seconds or HTTP date) into seconds from ``now``."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


class RetryableAttemptError(Exception):
    """An attempt that failed in a way worth retrying; carries its manifest record."""

    def __init__(self, record: PollRecord, retry_after: float | None) -> None:
        """Wrap the failed attempt's record and its ``Retry-After`` delay, if any."""
        super().__init__(record.error or f"HTTP {record.http_status}")
        self.record = record
        self.retry_after = retry_after


@dataclass
class Collector:
    """Polls the endpoint, stores raw bodies and writes the manifest."""

    cfg: CollectConfig
    raw_dir: Path
    manifest: ManifestWriter
    token: SecretStr
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    health: HealthMonitor = field(init=False)
    _not_before: datetime | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        """Set up health tracking from the configuration."""
        self.health = HealthMonitor(
            interval_s=self.cfg.interval_s,
            stale_after_identical=self.cfg.stale_after_identical,
            error_streak_alert=self.cfg.error_streak_alert,
            gap_factor=self.cfg.gap_factor,
        )

    def client(self) -> httpx.AsyncClient:
        """Create the HTTP client. The API key is added per request, never as a default."""
        return httpx.AsyncClient(
            timeout=self.cfg.timeout_s,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
        )

    async def attempt(self, client: httpx.AsyncClient, number: int) -> PollRecord:
        """Make one request, store its body and record it.

        Raises :class:`RetryableAttemptError` (after recording) if the attempt should be
        retried.
        """
        fetched_at = self.clock()
        start = time.perf_counter()
        try:
            response = await client.get(
                self.cfg.url,
                params=self.cfg.params,
                headers={AUTH_HEADER: self.token.get_secret_value()},
            )
            body = response.content
        except httpx.HTTPError as exc:
            record = PollRecord(
                fetched_at=fetched_at,
                attempt=number,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            self.manifest.add(record)
            raise RetryableAttemptError(record, None) from exc
        latency_ms = round((time.perf_counter() - start) * 1000, 1)

        digest = hashlib.sha256(body).hexdigest()
        relpath = raw_relpath(fetched_at, digest)
        stored = compress(body)
        write_atomic(self.raw_dir / relpath, stored)
        record = PollRecord(
            fetched_at=fetched_at,
            attempt=number,
            http_status=response.status_code,
            sha256=digest,
            bytes=len(body),
            stored_bytes=len(stored),
            latency_ms=latency_ms,
            path=relpath.as_posix(),
            error=None if response.status_code == 200 else f"HTTP {response.status_code}",
        )
        self.manifest.add(record)
        if response.status_code in RETRYABLE_STATUS:
            retry_after = parse_retry_after(response.headers.get("Retry-After"), self.clock())
            raise RetryableAttemptError(record, retry_after)
        return record

    def _wait(self, state: RetryCallState) -> float:
        backoff = min(
            self.cfg.backoff_max_s, self.cfg.backoff_initial_s * 2.0 ** (state.attempt_number - 1)
        )
        exc = state.outcome.exception() if state.outcome else None
        retry_after = exc.retry_after if isinstance(exc, RetryableAttemptError) else None
        return max(backoff, retry_after or 0.0)

    async def tick(self, client: httpx.AsyncClient, deadline: datetime) -> PollRecord:
        """Run one tick (with retries until ``deadline``) and return its final record."""

        def stop(state: RetryCallState) -> bool:
            if state.attempt_number >= self.cfg.max_attempts:
                return True
            return self.clock() + timedelta(seconds=self._wait(state)) >= deadline

        retrying = AsyncRetrying(
            retry=retry_if_exception_type(RetryableAttemptError),
            wait=self._wait,
            stop=stop,
            sleep=self.sleep,
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self.attempt(client, attempt.retry_state.attempt_number)
        except RetryableAttemptError as exc:
            if exc.retry_after:
                # Honour Retry-After across ticks, not only within this one.
                self._not_before = self.clock() + timedelta(seconds=exc.retry_after)
            return exc.record
        raise AssertionError("unreachable: tenacity always returns or raises")

    async def _report(self, final: PollRecord, notifier: Notifier) -> list[HealthEvent]:
        level = logging.INFO if final.ok else logging.WARNING
        log.log(
            level,
            "poll",
            extra={
                "status": final.http_status,
                "attempt": final.attempt,
                "bytes": final.bytes,
                "latency_ms": final.latency_ms,
                "path": final.path,
                "error": final.error,
            },
        )
        events = self.health.observe(final)
        for event in events:
            log.warning("health", extra={"event": event.kind, "detail": event.message})
            await notifier.send(event.message, title=f"headway: {event.kind}", priority="high")
        return events

    async def once(self) -> PollRecord:
        """Poll a single time (no alignment), flush the manifest and return the record."""
        async with self.client() as client:
            deadline = self.clock() + timedelta(seconds=self.cfg.interval_s)
            final = await self.tick(client, deadline)
            await self._report(final, Notifier(client, ""))
        self.manifest.flush()
        return final

    async def _wait_until(self, when: datetime, stop: asyncio.Event) -> bool:
        """Sleep until ``when``; return True if ``stop`` was set meanwhile."""
        delay = (when - self.clock()).total_seconds()
        if delay > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
        return stop.is_set()

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until ``stop`` is set, then finish the current tick and flush."""
        for tmp in remove_stale_tmp(self.raw_dir):
            log.warning("removed stale temporary file", extra={"path": str(tmp)})
        log.info(
            "collector started",
            extra={
                "url": self.cfg.url,
                "interval_s": self.cfg.interval_s,
                "raw_dir": str(self.raw_dir),
            },
        )
        async with self.client() as client:
            notifier = Notifier(client, self.cfg.ntfy_url)
            last_flush = last_heartbeat = self.clock()
            try:
                while not stop.is_set():
                    start = self.clock()
                    if self._not_before is not None and self._not_before > start:
                        start = self._not_before
                    tick_at = next_tick(start, self.cfg.interval_s)
                    if await self._wait_until(tick_at, stop):
                        break
                    deadline = tick_at + timedelta(seconds=self.cfg.interval_s)
                    await self._report(await self.tick(client, deadline), notifier)

                    now = self.clock()
                    if (now - last_flush).total_seconds() >= self.cfg.manifest_flush_s:
                        self.manifest.flush()
                        last_flush = now
                    if (now - last_heartbeat).total_seconds() >= self.cfg.heartbeat_interval_s:
                        message = (
                            f"ok={self.health.total_ok} failed={self.health.total_failed} "
                            f"last_success={self.health.last_success}"
                        )
                        log.info("heartbeat", extra={"detail": message})
                        await notifier.send(message, title="headway: heartbeat", priority="low")
                        last_heartbeat = now
            finally:
                self.manifest.flush()
                log.info(
                    "collector stopped",
                    extra={"ok": self.health.total_ok, "failed": self.health.total_failed},
                )
