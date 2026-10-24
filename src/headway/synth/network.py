"""Synthetic route network: shapes and stops in a local metric plane.

Coordinates are generated in metres east/north of a centre point and converted to WGS 84
with an equirectangular approximation, which is accurate to well under a metre over the
few kilometres involved. Bus routes start next to a tram stop so that transfers exist.
"""

import bisect
import math
import zlib
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import numpy.typing as npt

from headway.synth.config import NetworkConfig

EARTH_M_PER_DEG = 111_320.0
RouteKind = Literal["tram", "city_bus", "regional_bus"]
FloatArray = npt.NDArray[np.float64]


def rng_for(seed: int, *parts: str | int) -> np.random.Generator:
    """Return an independent, reproducible random stream for ``parts``.

    Streams depend only on their labels, never on the order in which they are created.
    """
    words = [seed & 0xFFFFFFFF]
    for part in parts:
        words.append(part & 0xFFFFFFFF if isinstance(part, int) else zlib.crc32(part.encode()))
    return np.random.default_rng(np.random.SeedSequence(words))


@dataclass(frozen=True)
class Plane:
    """Conversion between local metres and WGS 84 degrees around a centre point."""

    lat0: float
    lon0: float

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        """Return ``(lat, lon)`` of the point ``x`` m east and ``y`` m north."""
        lat = self.lat0 + y / EARTH_M_PER_DEG
        lon = self.lon0 + x / (EARTH_M_PER_DEG * math.cos(math.radians(self.lat0)))
        return lat, lon


@dataclass
class Path:
    """A polyline with cumulative distances, for one direction of a route."""

    xy: FloatArray  # shape (n, 2), metres
    dist: FloatArray = field(init=False)  # cumulative distance at each vertex
    _d: list[float] = field(init=False, repr=False)
    _x: list[float] = field(init=False, repr=False)
    _y: list[float] = field(init=False, repr=False)
    _units: list[tuple[float, float]] = field(init=False, repr=False)
    _bearings: list[int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Compute cumulative distances and per-segment directions (plain lists: fast)."""
        steps = np.hypot(*np.diff(self.xy, axis=0).T)
        self.dist = np.concatenate([[0.0], np.cumsum(steps)])
        self._d = self.dist.tolist()
        self._x = self.xy[:, 0].tolist()
        self._y = self.xy[:, 1].tolist()
        self._units = []
        self._bearings = []
        for i in range(len(self._d) - 1):
            dx, dy = self._x[i + 1] - self._x[i], self._y[i + 1] - self._y[i]
            norm = math.hypot(dx, dy) or 1.0
            self._units.append((dx / norm, dy / norm))
            self._bearings.append(round(math.degrees(math.atan2(dx, dy))) % 360)

    def _segment(self, d: float) -> int:
        return min(max(bisect.bisect_right(self._d, d) - 1, 0), len(self._d) - 2)

    @property
    def length(self) -> float:
        """Total length in metres."""
        return float(self.dist[-1])

    def point_at(self, d: float) -> tuple[float, float]:
        """Return the point at distance ``d`` along the path (clamped to its ends)."""
        d = min(max(d, 0.0), self._d[-1])
        i = self._segment(d)
        span = self._d[i + 1] - self._d[i]
        f = (d - self._d[i]) / span if span > 0 else 0.0
        return (
            self._x[i] + f * (self._x[i + 1] - self._x[i]),
            self._y[i] + f * (self._y[i + 1] - self._y[i]),
        )

    def heading_at(self, d: float) -> tuple[float, float]:
        """Return the unit direction vector of the segment containing ``d``."""
        return self._units[self._segment(d)]

    def bearing_at(self, d: float) -> int:
        """Return the compass bearing (0 = north, 90 = east) at ``d``, in whole degrees."""
        return self._bearings[self._segment(d)]


@dataclass(frozen=True)
class Stop:
    """A stop with its position."""

    stop_id: str
    name: str
    x: float
    y: float


@dataclass
class Direction:
    """One direction of a route: its path and the stops along it."""

    direction_id: int
    shape_id: str
    path: Path
    stops: list[Stop]
    stop_dist: FloatArray  # distance of each stop along the path


@dataclass
class Route:
    """A synthetic route with both directions."""

    route_id: str
    short_name: str
    route_type: int  # GTFS: 0 tram, 3 bus
    kind: RouteKind
    directions: tuple[Direction, Direction]


def _walk(
    rng: np.random.Generator, start: tuple[float, float], length: float, cfg: NetworkConfig
) -> FloatArray:
    heading = rng.uniform(0, 2 * math.pi)
    points = [start]
    n_steps = max(2, math.ceil(length / cfg.shape_step_m))
    step = length / n_steps
    for _ in range(n_steps):
        heading += math.radians(rng.uniform(-cfg.max_turn_deg, cfg.max_turn_deg))
        x, y = points[-1]
        points.append((x + step * math.sin(heading), y + step * math.cos(heading)))
    return np.array(points, dtype=np.float64)


def build_network(seed: int, cfg: NetworkConfig) -> list[Route]:
    """Build all routes deterministically from ``seed``."""
    n_regional = round(cfg.bus_routes * cfg.regional_bus_share)
    trams: list[RouteKind] = ["tram"] * cfg.tram_routes
    city: list[RouteKind] = ["city_bus"] * (cfg.bus_routes - n_regional)
    regional: list[RouteKind] = ["regional_bus"] * n_regional
    kinds = [*trams, *city, *regional]
    routes: list[Route] = []
    tram_stops: list[Stop] = []
    for index, kind in enumerate(kinds, start=1):
        rng = rng_for(seed, "route", index)
        n_stops = int(rng.integers(cfg.stops_per_route[0], cfg.stops_per_route[1] + 1))
        gaps = rng.uniform(*cfg.stop_spacing_m, size=n_stops - 1)
        if kind != "tram" and tram_stops:
            anchor = tram_stops[int(rng.integers(len(tram_stops)))]
            start = (anchor.x + 30.0, anchor.y + 30.0)  # a short walk from a tram stop
        else:
            r = cfg.radius_m * math.sqrt(rng.uniform())
            a = rng.uniform(0, 2 * math.pi)
            start = (r * math.cos(a), r * math.sin(a))
        forward = Path(_walk(rng, start, float(gaps.sum()), cfg))
        stop_d = np.concatenate([[0.0], np.cumsum(gaps)]) * (forward.length / gaps.sum())
        route_id = f"L{index}"
        stops = []
        for i, d in enumerate(stop_d, start=1):
            x, y = forward.point_at(float(d))
            stops.append(Stop(f"SYN{index:02d}S{i:02d}", f"Synthetic {index}-{i}", x, y))
        if kind == "tram":
            tram_stops += stops
        backward = Path(forward.xy[::-1].copy())
        routes.append(
            Route(
                route_id=route_id,
                short_name=str(index),
                route_type=0 if kind == "tram" else 3,
                kind=kind,
                directions=(
                    Direction(0, f"{route_id}V0", forward, stops, stop_d),
                    Direction(
                        1, f"{route_id}V1", backward, stops[::-1], forward.length - stop_d[::-1]
                    ),
                ),
            )
        )
    return routes
