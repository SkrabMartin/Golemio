"""Findings about the collector itself, from the manifest of one service day."""

import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta

from headway.audit.config import CollectionThresholds
from headway.audit.findings import Finding
from headway.collect.manifest import PollRecord

UTC_STAMP = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class CollectionStats:
    """Numbers behind the coverage line of the summary."""

    attempts: int
    ok_polls: int
    first: datetime | None
    last: datetime | None
    last_ok: datetime | None
    expected_polls: int
    failed_ticks: int
    gap_seconds: float

    @property
    def coverage(self) -> float | None:
        """Share of the polls expected between the first and last record that succeeded."""
        return None if self.expected_polls == 0 else min(1.0, self.ok_polls / self.expected_polls)


def _ticks(records: list[PollRecord]) -> list[list[PollRecord]]:
    """Group attempts into ticks: a new tick starts at every first attempt."""
    ticks: list[list[PollRecord]] = []
    for record in records:
        if record.attempt == 1 or not ticks:
            ticks.append([record])
        else:
            ticks[-1].append(record)
    return ticks


def _status(record: PollRecord) -> str:
    if record.http_status is not None:
        return str(record.http_status)
    return record.error or "no response"


def _error_runs(ticks: list[list[PollRecord]], min_ticks: int) -> list[list[list[PollRecord]]]:
    """Return the runs of consecutive ticks in which no attempt succeeded."""
    runs: list[list[list[PollRecord]]] = []
    current: list[list[PollRecord]] = []
    for tick in ticks:
        if any(r.ok for r in tick):
            if len(current) >= min_ticks:
                runs.append(current)
            current = []
        else:
            current.append(tick)
    if len(current) >= min_ticks:
        runs.append(current)
    return runs


def collection_findings(
    records: list[PollRecord], interval_s: float, th: CollectionThresholds
) -> tuple[list[Finding], CollectionStats]:
    """Find gaps, API error runs and stale feeds among the attempts of one day."""
    records = sorted(records, key=lambda r: (r.fetched_at, r.attempt))
    findings: list[Finding] = []
    ticks = _ticks(records)

    spans: list[tuple[datetime, datetime]] = []
    for run in _error_runs(ticks, th.api_error_min_ticks):
        statuses: dict[str, int] = {}
        for tick in run:
            for r in tick:
                statuses[_status(r)] = statuses.get(_status(r), 0) + 1
        start, end = run[0][0].fetched_at, run[-1][-1].fetched_at
        spans.append((start, end))
        results = ", ".join(f"{k} x{v}" for k, v in sorted(statuses.items()))
        findings.append(
            Finding(
                "api_errors",
                "poll",
                start.strftime(UTC_STAMP),
                start,
                end,
                float(len(run)),
                f"{len(run)} failed poll(s) in a row; attempts by result: {results}",
            )
        )

    ok = [r for r in records if r.ok]
    gap_total = 0.0
    for a, b in itertools.pairwise(ok):
        seconds = (b.fetched_at - a.fetched_at).total_seconds()
        if seconds <= th.gap_factor * interval_s + th.gap_tolerance_s:
            continue
        gap_total += seconds
        failed = any(a.fetched_at <= s <= b.fetched_at for s, _ in spans)
        cause = "failed polls" if failed else "no requests were recorded (collector stopped?)"
        findings.append(
            Finding(
                "collection_gap",
                "poll",
                a.fetched_at.strftime(UTC_STAMP),
                a.fetched_at,
                b.fetched_at,
                seconds,
                f"no successful poll for {seconds / 60:.1f} min "
                f"({seconds / interval_s:.1f} x the {interval_s:.0f} s interval); cause: {cause}",
            )
        )

    first_of_run = 0
    for i in range(1, len(ok) + 1):
        if i < len(ok) and ok[i].sha256 == ok[first_of_run].sha256:
            continue
        n = i - first_of_run
        if n >= th.stale_feed_min_polls:
            first, last = ok[first_of_run].fetched_at, ok[i - 1].fetched_at
            findings.append(
                Finding(
                    "stale_feed",
                    "poll",
                    first.strftime(UTC_STAMP),
                    first,
                    last,
                    float(n),
                    f"{n} identical responses in a row over {(last - first).total_seconds():.0f} s",
                )
            )
        first_of_run = i

    first_ts = records[0].fetched_at if records else None
    last_ts = records[-1].fetched_at if records else None
    expected = 0
    if first_ts is not None and last_ts is not None:
        expected = round((last_ts - first_ts) / timedelta(seconds=interval_s)) + 1
    failed_ticks = sum(1 for t in ticks if not any(r.ok for r in t))
    last_ok = ok[-1].fetched_at if ok else None
    stats = CollectionStats(
        len(records), len(ok), first_ts, last_ts, last_ok, expected, failed_ticks, gap_total
    )
    return findings, stats
