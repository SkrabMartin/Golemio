"""Health tracking for the collector: stale feed, error streaks and gaps."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from headway.collect.manifest import PollRecord


@dataclass(frozen=True)
class HealthEvent:
    """A change in collector health worth logging (and possibly notifying)."""

    kind: str  # stale_feed, stale_recovered, error_streak, errors_recovered, gap
    message: str
    at: datetime


@dataclass
class HealthMonitor:
    """Turns the outcome of each tick into health events.

    Each event fires once when a condition starts; recovery is reported once when it ends.
    """

    interval_s: int
    stale_after_identical: int
    error_streak_alert: int
    gap_factor: float
    identical_count: int = 0
    error_streak: int = 0
    last_sha: str | None = None
    last_success: datetime | None = None
    total_ok: int = 0
    total_failed: int = 0
    _stale: bool = field(default=False, repr=False)
    _erroring: bool = field(default=False, repr=False)

    def observe(self, final: PollRecord) -> list[HealthEvent]:
        """Record the final attempt of one tick and return the resulting events."""
        events: list[HealthEvent] = []
        at = final.fetched_at
        if not final.ok:
            self.total_failed += 1
            self.error_streak += 1
            if self.error_streak >= self.error_streak_alert and not self._erroring:
                self._erroring = True
                events.append(
                    HealthEvent(
                        "error_streak",
                        f"{self.error_streak} failed ticks in a row; last: "
                        f"status={final.http_status} error={final.error}",
                        at,
                    )
                )
            return events

        self.total_ok += 1
        if self._erroring:
            events.append(
                HealthEvent("errors_recovered", f"recovered after {self.error_streak} ticks", at)
            )
        self._erroring = False
        self.error_streak = 0

        limit = timedelta(seconds=self.gap_factor * self.interval_s)
        if self.last_success is not None and at - self.last_success > limit:
            gap = (at - self.last_success).total_seconds()
            events.append(HealthEvent("gap", f"no successful poll for {gap:.0f} s", at))
        self.last_success = at

        if final.sha256 == self.last_sha:
            self.identical_count += 1
        else:
            if self._stale:
                events.append(
                    HealthEvent(
                        "stale_recovered", f"feed changed after {self.identical_count} polls", at
                    )
                )
            self._stale = False
            self.identical_count = 1
            self.last_sha = final.sha256
        if self.identical_count >= self.stale_after_identical and not self._stale:
            self._stale = True
            events.append(
                HealthEvent("stale_feed", f"{self.identical_count} identical bodies in a row", at)
            )
        return events
