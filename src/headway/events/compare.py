"""Comparison of reconstructed delays with the delay the API reports."""

from typing import Any

import numpy as np
import polars as pl

from headway.events.reconstruct import FloatArray, StopEvent


def stop_position(index: int, count: int) -> str:
    """Return ``first``, ``middle`` or ``last`` for stop ``index`` of ``count``."""
    if index == 0:
        return "first"
    return "last" if index == count - 1 else "middle"


def nearest_reported(
    times: FloatArray, reported: FloatArray, at: float, max_dt_s: float
) -> tuple[float, float] | None:
    """Return ``(reported delay, time gap)`` of the position nearest to ``at``, if close enough.

    ``times`` must be sorted; NaN in ``reported`` means the API gave no delay there.
    """
    i = int(np.searchsorted(times, at))
    best: tuple[float, float] | None = None
    for j in (i - 1, i):
        if 0 <= j < len(times) and np.isfinite(reported[j]):
            gap = abs(float(times[j]) - at)
            if gap <= max_dt_s and (best is None or gap < best[1]):
                best = (float(reported[j]), gap)
    return best


def event_time_and_delay(
    event: StopEvent, arr_delay: float | None, dep_delay: float | None
) -> tuple[float, float] | None:
    """Return the time and delay to compare for an event: arrival if known, else departure."""
    if event.arr is not None and arr_delay is not None:
        return event.arr, arr_delay
    if event.dep is not None and dep_delay is not None:
        return event.dep, dep_delay
    return None


def _group(df: pl.DataFrame, by: str) -> list[dict[str, Any]]:
    rows = (
        df.group_by(by)
        .agg(
            pl.len().alias("n"),
            pl.col("diff_s").median().alias("median_diff_s"),
            pl.col("diff_s").quantile(0.1).alias("p10_diff_s"),
            pl.col("diff_s").quantile(0.9).alias("p90_diff_s"),
            pl.col("diff_s").abs().mean().alias("mean_abs_diff_s"),
        )
        .sort(by)
    )
    return [
        {k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items()}
        for r in rows.iter_rows(named=True)
    ]


def summarize(df: pl.DataFrame) -> dict[str, Any]:
    """Summarise ``reported - reconstructed`` by route type, stop position and route.

    A positive difference means the API reports the vehicle as more delayed than the
    positions show. Every row carries its sample size ``n``.
    """
    if df.height == 0:
        return {"n": 0}
    return {
        "n": df.height,
        "median_diff_s": round(float(df.select(pl.col("diff_s").median()).item()), 1),
        "mean_abs_diff_s": round(float(df.select(pl.col("diff_s").abs().mean()).item()), 1),
        "by_route_type": _group(df, "route_type"),
        "by_stop_position": _group(df, "stop_position"),
        "by_route": _group(df, "route_id"),
    }
