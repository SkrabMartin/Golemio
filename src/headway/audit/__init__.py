"""Layer L2: data-quality audit.

Produces a findings table and a daily summary that describe the quality of the collected
data: collection gaps, stale positions, GPS jumps, positions far from the trip shape,
vehicles assigned to two trips at once and trips that go silent. All thresholds come from
configuration and are reported alongside the results. The audit only reports problems; it
never modifies data.
"""
