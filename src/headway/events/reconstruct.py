"""Arrival and departure times at stops from a series of (time, distance along the route).

Pure functions on arrays, so the method can be tested without any data files. Times are
seconds (any origin), distances metres along the trip shape.

Two situations occur at a stop:

- The vehicle is seen at the stop: a *plateau* of positions (one is enough) with nearly the
  same distance. The arrival lies between the last position before the plateau and
  its first position; the departure between its last position and the next one.
- The vehicle is not seen near the stop (the sampling skipped it): the time it passed the
  stop is interpolated between the two positions that
  bracket the stop, and arrival and departure are the same.

Every time carries an uncertainty of half the gap between the positions that bracket it.
"""

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from headway.events.project import isotonic_increasing

FloatArray = npt.NDArray[np.float64]

MIN_SPEED_MPS = 0.5  # slower than this is not a speed to extrapolate with


@dataclass(frozen=True)
class StopEvent:
    """Reconstructed times at one stop (``None`` where the data does not reach)."""

    stop_index: int
    arr: float | None
    dep: float | None
    arr_uncert: float | None
    dep_uncert: float | None
    n_positions: int
    method: str  # "plateau" or "interpolated"


def find_plateaus(d: FloatArray, eps_m: float) -> list[tuple[int, int]]:
    """Return the ``(first, last)`` indices of runs of positions at one place.

    A run holds positions whose distance varies by at most ``eps_m``; a single position is a
    run of one. Neighbouring runs whose centres are within ``eps_m`` are one place: GPS noise
    makes a long stop wander far enough to split into pieces otherwise, and then the departure
    would be read from the first piece. ``d`` must be non-decreasing.
    """
    runs: list[tuple[int, int]] = []
    n, i = len(d), 0
    while i < n:
        j = i
        while j + 1 < n and d[j + 1] - d[i] <= eps_m:
            j += 1
        runs.append((i, j))
        i = j + 1
    merged = [runs[0]] if runs else []
    for first, last in runs[1:]:
        before = merged[-1]
        centre_before = float(np.median(d[before[0] : before[1] + 1]))
        if float(np.median(d[first : last + 1])) - centre_before <= eps_m:
            merged[-1] = (before[0], last)
        else:
            merged.append((first, last))
    return merged


def _speed(t: FloatArray, d: FloatArray, i: int) -> float | None:
    """Speed between positions ``i`` and ``i + 1``, or None if absent or too slow."""
    if i < 0 or i + 1 >= len(t):
        return None
    dt = t[i + 1] - t[i]
    speed = (d[i + 1] - d[i]) / dt if dt > 0 else 0.0
    return speed if speed >= MIN_SPEED_MPS else None


def _arrival(
    t: FloatArray, d: FloatArray, first: int, plateau_d: float, stop_d: float
) -> tuple[float, float] | None:
    """Arrival time and uncertainty for a plateau starting at position ``first``.

    The vehicle reached the plateau between the position before it and its first position.
    If the plateau lies short of the stop, a vehicle that was only passing may still have
    needed time to reach the stop, which widens the uncertainty.
    """
    if first == 0:
        return None  # the vehicle was already standing when it was first seen
    before, gap = first - 1, t[first] - t[first - 1]
    speed = _speed(t, d, first - 2)  # the speed it was driving at before the stop
    estimate = t[before] + (gap / 2 if speed is None else (plateau_d - d[before]) / speed)
    approach = speed or _speed(t, d, before)
    short = max(0.0, stop_d - plateau_d)
    extra = 0.0 if approach is None else short / approach
    return float(np.clip(estimate, t[before], t[first])), gap / 2 + extra


def _departure(
    t: FloatArray, d: FloatArray, last: int, plateau_d: float, stop_d: float
) -> tuple[float, float] | None:
    """Departure time and uncertainty for a plateau ending at position ``last``.

    The vehicle left the plateau between its last position and the next one. If the plateau
    lies beyond the stop, a vehicle that was only passing may already have left the stop.
    """
    if last == len(t) - 1:
        return None  # still standing when last seen
    after, gap = last + 1, t[last + 1] - t[last]
    speed = _speed(t, d, after)  # the speed it drives at after leaving
    estimate = t[after] - (gap / 2 if speed is None else (d[after] - plateau_d) / speed)
    leaving = speed or _speed(t, d, last)
    past = max(0.0, plateau_d - stop_d)
    extra = 0.0 if leaving is None else past / leaving
    return float(np.clip(estimate, t[last], t[after])), gap / 2 + extra


def _better(d: FloatArray, stop_d: float, new: tuple[int, int], held: tuple[int, int]) -> bool:
    """True if plateau ``new`` serves the stop better than ``held``: longer, else nearer."""
    new_len, held_len = new[1] - new[0], held[1] - held[0]
    if new_len != held_len:
        return new_len > held_len
    return abs(float(np.median(d[new[0] : new[1] + 1])) - stop_d) < abs(
        float(np.median(d[held[0] : held[1] + 1])) - stop_d
    )


def reconstruct_events(
    t: FloatArray,
    d: FloatArray,
    stop_d: FloatArray,
    radius_m: float,
    eps_m: float,
) -> list[StopEvent]:
    """Reconstruct the events of one trip.

    ``t`` and ``d`` are the positions in time order; ``stop_d`` the distance of every stop along
    the shape, in stop order. Stops that the positions do not reach get no event.
    """
    d = isotonic_increasing(d)
    plateaus = find_plateaus(d, eps_m)

    owner: dict[int, tuple[int, int]] = {}  # stop index -> its plateau
    for first, last in plateaus:
        centre = float(np.median(d[first : last + 1]))
        stop = int(np.argmin(np.abs(stop_d - centre)))
        if abs(stop_d[stop] - centre) > radius_m:
            continue  # standing somewhere that is not a stop (a signal, a queue)
        held = owner.get(stop)
        if held is None or _better(d, stop_d[stop], (first, last), held):
            owner[stop] = (first, last)

    events: list[StopEvent] = []
    for stop, position in enumerate(stop_d):
        if stop in owner:
            first, last = owner[stop]
            centre = float(np.median(d[first : last + 1]))
            arrival = _arrival(t, d, first, centre, float(position))
            departure = _departure(t, d, last, centre, float(position))
            events.append(
                StopEvent(
                    stop,
                    None if arrival is None else arrival[0],
                    None if departure is None else departure[0],
                    None if arrival is None else arrival[1],
                    None if departure is None else departure[1],
                    last - first + 1,
                    "plateau",
                )
            )
        elif d[0] < position <= d[-1]:
            j = int(np.searchsorted(d, position, side="left")) - 1
            frac = (position - d[j]) / (d[j + 1] - d[j])
            passed = float(t[j] + frac * (t[j + 1] - t[j]))
            half_gap = float(t[j + 1] - t[j]) / 2
            events.append(StopEvent(stop, passed, passed, half_gap, half_gap, 2, "interpolated"))
    return events
