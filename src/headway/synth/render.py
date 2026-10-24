"""Render a vehicle message as a feature of the ``GET /v2/vehiclepositions`` response.

Field semantics follow docs/api.md and the null patterns observed in the probe:

- the city operator ("SYNTH CITY TRANSIT", like DP PRAHA) has null ``speed``, null
  ``is_canceled`` and null CIS fields, and reports delays to the second;
- the regional operator has speed, ``is_canceled = false``, CIS ids, and last-stop delays
  rounded to whole minutes;
- ``delay.last_stop_arrival`` is null when the stop's scheduled arrival equals its
  departure; ``delay.last_stop_departure`` is null at the terminal stop and (ASSUMPTION)
  while the vehicle is still standing at the stop;
- timestamps carry the Europe/Prague offset, as the real API returned them.
"""

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from headway.synth.config import ReportingConfig
from headway.synth.gtfs_writer import AGENCIES
from headway.synth.network import Plane
from headway.synth.simulate import Reports, TripRun

VEHICLE_TYPES = {
    "tram": {"description_cs": "tramvaj", "description_en": "tram", "id": 2},
    "city_bus": {"description_cs": "autobus", "description_en": "bus", "id": 3},
    "regional_bus": {
        "description_cs": "regionální autobus",
        "description_en": "regional bus",
        "id": 4,
    },
}


class Renderer:
    """Turns (trip, message index) into feature dictionaries."""

    def __init__(self, plane: Plane, cfg: ReportingConfig, bias_route: str, tz: str) -> None:
        """Set the coordinate plane, reporting parameters and the route with a planted bias."""
        self.plane = plane
        self.cfg = cfg
        self.bias_route = bias_route
        self.tz = ZoneInfo(tz)

    def iso(self, ts: float) -> str:
        """Local ISO 8601 time with offset, to the second."""
        return datetime.fromtimestamp(math.floor(float(ts) + 0.5), self.tz).isoformat(
            timespec="seconds"
        )

    def _round(self, run: TripRun, seconds: float) -> int:
        if run.plan.route.kind == "regional_bus":
            step = self.cfg.regional_delay_rounding_s
            return int(round(seconds / step) * step)
        return round(seconds)

    def feature(self, run: TripRun, reports: Reports, k: int) -> dict[str, Any]:
        """Render message ``k`` of ``run``."""
        plan, route = run.plan, run.plan.route
        t = float(reports.t[k])
        nx, ny = float(reports.noise_xy[k, 0]), float(reports.noise_xy[k, 1])
        path = plan.direction.path
        d = run.distance_at(t)
        x, y = path.point_at(d)
        lat, lon = self.plane.to_latlon(x + nx, y + ny)
        n = len(run.sched_arr)
        i = run.stop_index_at(t)
        nxt = min(i + 1, n - 1)
        dwelling = run.dwelling_at(t)
        city = route.kind != "regional_bus"

        delay = run.true_delay_at(t) + float(reports.golemio_noise[k])
        if route.route_id == self.bias_route:
            delay += self.cfg.golemio_delay_bias_s
        arrival_delay = (
            None
            if i == 0 or run.sched_arr[i] == run.sched_dep[i]
            else self._round(run, run.true_arr[i] - run.sched_arr[i])
        )
        departed = t >= run.true_dep[i] and dwelling is None
        departure_delay = (
            self._round(run, run.true_dep[i] - run.sched_dep[i]) if i < n - 1 and departed else None
        )
        if city:
            speed = None
        elif dwelling is not None:
            speed = 0
        else:
            seg_time = run.true_arr[i + 1] - run.true_dep[i]
            seg_len = plan.direction.stop_dist[i + 1] - plan.direction.stop_dist[i]
            speed = round(seg_len / seg_time * 3.6) if seg_time > 0 else 0
        stops = plan.direction.stops
        reg = plan.vehicle_reg
        agency = AGENCIES[route.kind][1]
        return {
            "geometry": {"coordinates": [round(lon, 5), round(lat, 5)], "type": "Point"},
            "properties": {
                "last_position": {
                    "bearing": path.bearing_at(d),
                    "delay": {
                        "actual": round(delay),
                        "last_stop_arrival": arrival_delay,
                        "last_stop_departure": departure_delay,
                    },
                    "is_canceled": None if city else False,
                    "last_stop": self._stops(run)[i],
                    "next_stop": self._stops(run)[nxt],
                    "origin_timestamp": self.iso(t),
                    "shape_dist_traveled": f"{d / 1000:.6f}".rstrip("0").rstrip(".") or "0",
                    "speed": speed,
                    "state_position": "at_stop" if dwelling is not None else "on_track",
                    "tracking": True,
                },
                "trip": {
                    "agency_name": {"real": agency, "scheduled": agency},
                    "air_conditioned": reg % 4 != 0,
                    "cis": {
                        "line_id": None if city else f"9{int(route.short_name):05d}",
                        "trip_number": None if city else 4000 + plan.block_seq,
                    },
                    "gtfs": {
                        "route_id": route.route_id,
                        "route_short_name": route.short_name,
                        "route_type": route.route_type,
                        "trip_headsign": stops[-1].name,
                        "trip_id": plan.trip_id,
                        "trip_short_name": None,
                    },
                    "origin_route_name": route.short_name,
                    "sequence_id": plan.block_seq,
                    "start_timestamp": self._stops(run)[0]["departure_time"],
                    "usb_chargers": reg % 5 == 0,
                    "vehicle_registration_number": reg,
                    "vehicle_type": VEHICLE_TYPES[route.kind],
                    "wheelchair_accessible": True,
                },
            },
            "type": "Feature",
        }

    def _stops(self, run: TripRun) -> list[dict[str, Any]]:
        # Scheduled stop dicts never change for a trip, so they are built once. Features
        # are serialised right after rendering, so sharing the dicts is safe.
        if run.stop_json is None:
            run.stop_json = [
                {
                    "arrival_time": self.iso(float(run.sched_arr[i])),
                    "departure_time": self.iso(float(run.sched_dep[i])),
                    "id": stop.stop_id,
                    "sequence": i + 1,
                }
                for i, stop in enumerate(run.plan.direction.stops)
            ]
        return run.stop_json
