"""Distances in a metric coordinate system."""

import numpy as np
import numpy.typing as npt
from pyproj import Transformer

FloatArray = npt.NDArray[np.float64]

# UTM zone 33N covers Prague; its scale error there is below 0.1 %.
METRIC_CRS = "EPSG:32633"
_TO_METRIC = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)


def to_metric(lon: FloatArray, lat: FloatArray) -> FloatArray:
    """Convert WGS 84 arrays to an ``(n, 2)`` array of metres in :data:`METRIC_CRS`."""
    x, y = _TO_METRIC.transform(lon, lat)
    return np.column_stack([np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64)])


def distance_to_polyline(points: FloatArray, line: FloatArray) -> FloatArray:
    """Return the distance of each of ``points`` (n, 2) to the polyline ``line`` (m, 2)."""
    if len(line) == 1:
        return np.asarray(np.linalg.norm(points - line[0], axis=1), dtype=np.float64)
    a, b = line[:-1], line[1:]
    ab = b - a
    length2 = np.where((ab**2).sum(axis=1) == 0, 1.0, (ab**2).sum(axis=1))
    ap = points[:, None, :] - a[None, :, :]
    t = np.clip((ap * ab[None, :, :]).sum(axis=2) / length2[None, :], 0.0, 1.0)
    nearest = a[None, :, :] + t[:, :, None] * ab[None, :, :]
    distances = np.linalg.norm(points[:, None, :] - nearest, axis=2)
    return np.asarray(distances.min(axis=1), dtype=np.float64)
