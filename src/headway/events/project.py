"""Projection of positions onto a trip shape, and monotone smoothing of the result."""

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]


def cumulative_length(line: FloatArray) -> FloatArray:
    """Return the distance along ``line`` (m, shape (m, 2)) at each of its vertices."""
    steps = np.linalg.norm(np.diff(line, axis=0), axis=1)
    return np.concatenate([[0.0], np.cumsum(steps)])


def _closest(point: FloatArray, a: FloatArray, b: FloatArray) -> tuple[float, int, float]:
    """Return (offset, segment, fraction) of the closest point on segments a[i]-b[i]."""
    ab = b - a
    length2 = np.where((ab**2).sum(axis=1) == 0, 1.0, (ab**2).sum(axis=1))
    t = np.clip(((point - a) * ab).sum(axis=1) / length2, 0.0, 1.0)
    distance = np.linalg.norm(point - (a + t[:, None] * ab), axis=1)
    i = int(np.argmin(distance))
    return float(distance[i]), i, float(t[i])


def project_along(
    points: FloatArray,
    line: FloatArray,
    cumulative: FloatArray,
    hints: FloatArray | None,
    window_m: float,
    max_offset_m: float,
) -> tuple[FloatArray, FloatArray]:
    """Project each point onto the polyline; return (distance along it, offset from it) in m.

    ``hints`` (same length, NaN where unknown) are distances along the shape reported by the
    API. A point is projected onto the part of the shape within ``window_m`` of its hint, so a
    loop that passes the same place twice is not confused. If that part holds no point within
    ``max_offset_m``, the whole shape is searched.
    """
    n = len(points)
    along = np.empty(n)
    offset = np.empty(n)
    starts, ends = line[:-1], line[1:]
    seg_len = np.diff(cumulative)
    for k in range(n):
        result: tuple[float, int, float] | None = None
        hint = np.nan if hints is None else hints[k]
        if np.isfinite(hint):
            lo = max(int(np.searchsorted(cumulative, hint - window_m)) - 1, 0)
            hi = min(int(np.searchsorted(cumulative, hint + window_m)) + 1, len(starts))
            if lo < hi:
                d, i, t = _closest(points[k], starts[lo:hi], ends[lo:hi])
                if d <= max_offset_m:
                    result = (d, lo + i, t)
        if result is None:
            result = _closest(points[k], starts, ends)
        d, i, t = result
        offset[k] = d
        along[k] = cumulative[i] + t * seg_len[i]
    return along, offset


def isotonic_increasing(y: FloatArray) -> FloatArray:
    """Return the non-decreasing sequence closest to ``y`` in the least-squares sense.

    Pool-adjacent-violators: a vehicle does not drive backwards along its route, so a dip in
    the projected distance is noise, and it is averaged with its neighbours instead of
    being allowed to distort the times (a running maximum would let one spike push every
    later value up).
    """
    values: list[float] = []
    weights: list[int] = []
    for v in y.tolist():
        values.append(v)
        weights.append(1)
        while len(values) > 1 and values[-2] > values[-1]:
            w = weights[-2] + weights[-1]
            values[-2:] = [(values[-2] * weights[-2] + values[-1] * weights[-1]) / w]
            weights[-2:] = [w]
    return np.repeat(np.array(values), weights)
