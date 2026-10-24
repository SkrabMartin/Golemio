"""Layer L0: collection of raw vehicle-position snapshots.

Polls the Golemio vehicle-positions endpoint at a fixed, wall-clock-aligned interval and
stores every response body unmodified as a compressed file, written atomically and named
by fetch time and content hash. A daily manifest records the fetch time, HTTP status,
checksum, size and latency of each poll, and a health log reports stale feeds, error
streaks and gaps. The collector performs no parsing, so collection is isolated from any
later parser bug.
"""
