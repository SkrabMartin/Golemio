"""Generate synthetic Golemio vehicle-positions fixtures, deterministically.

The committed fixtures in ``tests/fixtures/golemio/`` are synthetic: every value is
invented by this script, while the structure (keys, JSON types, null patterns and the
edge cases observed in the real API, see ``docs/api.md``) matches the real responses.
Real probe samples are not redistributed; they can be rebuilt locally with
``scripts/make_golemio_fixtures.py``.

Covered cases, per poll:

- every route type seen in the probe (tram, metro, rail, bus, ferry, trolleybus);
- trains with null registration number and vehicle type, a train number and the
  ``"none"`` CIS line id;
- city-operator trips with null CIS fields and ``is_canceled``;
- null ``speed`` and null last-stop delays;
- positions ``on_track``, ``at_stop`` and ``off_track``;
- two vehicles of different route types sharing a registration number;
- one vehicle reported on two consecutive trips with the same message (trip handover).

Usage::

    uv run python scripts/make_synthetic_golemio_fixtures.py
"""

import argparse
import json
import random
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path("tests/fixtures/golemio")
SEED = 20261024
POLL_TIMES = (
    datetime(2026, 10, 24, 10, 48, 28, tzinfo=UTC),
    datetime(2026, 10, 24, 10, 48, 58, tzinfo=UTC),
    datetime(2026, 10, 24, 10, 49, 28, tzinfo=UTC),
)
LOCAL = timezone(timedelta(hours=2))  # the offset the real API used on this date

# route_type: (vehicle type id, Czech, English) as observed, or None (trains, ferry).
VEHICLE_TYPES: dict[int, tuple[int, str, str] | None] = {
    0: (2, "tramvaj", "tram"),
    1: (1, "metro", "metro"),
    2: None,
    3: (3, "autobus", "bus"),
    4: None,  # not observed in the probe
    11: (18, "trolejbus", "trolleybus"),
}
CITY_OPERATOR = "SYNTH CITY TRANSIT"


def _iso(ts: datetime) -> str:
    return ts.astimezone(LOCAL).isoformat(timespec="seconds")


def _trip_spec(index: int, rng: random.Random) -> dict[str, Any]:
    route_types = [0, 0, 0, 1, 2, 2, 3, 3, 3, 3, 3, 4, 11, 0, 3, 3, 0, 3]
    route_type = route_types[index % len(route_types)]
    is_train = route_type == 2
    city = route_type in (0, 1, 11) or (route_type == 3 and index % 2 == 0)
    line = f"{route_type}{index:02d}"
    return {
        "index": index,
        "route_type": route_type,
        "is_train": is_train,
        "city": city,
        "line": line,
        "trip_id": f"S{line}_{100 + index}_261024",
        "reg": None if is_train else 7000 + index,
        "start": POLL_TIMES[0] - timedelta(minutes=5 + 3 * index),
        "lon": 14.30 + rng.random() * 0.35,
        "lat": 49.98 + rng.random() * 0.17,
        "heading": rng.randrange(0, 360),
        "delay": rng.randrange(-60, 300),
        "stop": 2 + index % 9,
    }


def _feature(spec: dict[str, Any], poll: int, fetched_at: datetime) -> dict[str, Any]:
    i = spec["index"]
    origin = fetched_at - timedelta(seconds=5 + (i * 7) % 60)
    stop = spec["stop"] + (1 if poll == 2 and i % 3 == 0 else 0)
    state = "off_track" if i == 5 else ("at_stop" if (i + poll) % 3 == 0 else "on_track")
    vehicle_type = VEHICLE_TYPES[spec["route_type"]]
    last_sched = spec["start"] + timedelta(minutes=2 * (stop - 1))
    next_sched = last_sched + timedelta(minutes=2)
    terminal = stop == 2 and i % 4 == 0
    return {
        "geometry": {
            "coordinates": [
                round(spec["lon"] + poll * 0.0011, 5),
                round(spec["lat"] + poll * 0.0007, 5),
            ],
            "type": "Point",
        },
        "properties": {
            "last_position": {
                "bearing": spec["heading"],
                "delay": {
                    "actual": spec["delay"] + 4 * poll,
                    "last_stop_arrival": None if i % 7 == 0 else spec["delay"],
                    "last_stop_departure": None if terminal or i % 3 == 1 else spec["delay"],
                },
                "is_canceled": None if spec["city"] else False,
                "last_stop": {
                    "arrival_time": _iso(last_sched),
                    "departure_time": _iso(last_sched),
                    "id": f"SYN{i:03d}S{stop:02d}",
                    "sequence": stop,
                },
                "next_stop": {
                    "arrival_time": _iso(next_sched),
                    "departure_time": _iso(next_sched),
                    "id": f"SYN{i:03d}S{stop + 1:02d}",
                    "sequence": stop + 1,
                },
                "origin_timestamp": _iso(origin),
                "shape_dist_traveled": f"{0.85 * (stop - 1) + 0.12 * poll:.3f}",
                "speed": None if spec["city"] or state == "at_stop" else 20 + i,
                "state_position": state,
                "tracking": True,
            },
            "trip": {
                "agency_name": {
                    "real": None if spec["is_train"] else _agency(spec),
                    "scheduled": "SYNTH RAIL" if spec["is_train"] else _agency(spec),
                },
                "air_conditioned": None if spec["is_train"] else i % 4 != 0,
                "cis": {
                    "line_id": None
                    if spec["city"]
                    else ("none" if spec["is_train"] else f"9{i:05d}"),
                    "trip_number": None if spec["city"] else 4000 + i,
                },
                "gtfs": {
                    "route_id": f"L{spec['line']}",
                    "route_short_name": spec["line"],
                    "route_type": spec["route_type"],
                    "trip_headsign": f"Synthetic Terminus {i % 5 + 1}",
                    "trip_id": spec["trip_id"],
                    "trip_short_name": f"Os {9000 + i}" if spec["is_train"] else None,
                },
                "origin_route_name": None if spec["is_train"] else spec["line"],
                "sequence_id": None if spec["is_train"] else 1 + i % 6,
                "start_timestamp": _iso(spec["start"]),
                "usb_chargers": None if spec["is_train"] else i % 5 == 0,
                "vehicle_registration_number": spec["reg"],
                "vehicle_type": None
                if vehicle_type is None
                else {
                    "description_cs": vehicle_type[1],
                    "description_en": vehicle_type[2],
                    "id": vehicle_type[0],
                },
                "wheelchair_accessible": i % 9 != 4,
            },
        },
        "type": "Feature",
    }


def _agency(spec: dict[str, Any]) -> str:
    return CITY_OPERATOR if spec["city"] else f"SYNTH REGIONAL {spec['index'] % 3 + 1}"


def build_polls(n_trips: int = 18) -> list[dict[str, Any]]:
    """Return one synthetic FeatureCollection per poll time."""
    rng = random.Random(SEED)
    specs = [_trip_spec(i, rng) for i in range(n_trips)]
    # Two vehicles of different route types share a registration number.
    tram = next(s for s in specs if s["route_type"] == 0)
    bus = next(s for s in specs if s["route_type"] == 3 and not s["city"])
    bus["reg"] = tram["reg"]

    polls = []
    for poll, fetched_at in enumerate(POLL_TIMES):
        features = [_feature(spec, poll, fetched_at) for spec in specs]
        if poll == 0:
            # Trip handover: the same message appears on the vehicle's next trip.
            handover = json.loads(json.dumps(features[bus["index"]]))
            props = handover["properties"]
            props["trip"]["gtfs"]["trip_id"] = bus["trip_id"].replace(
                f"_{100 + bus['index']}_", f"_{101 + bus['index']}_"
            )
            props["last_position"]["state_position"] = "at_stop"
            props["last_position"]["last_stop"]["sequence"] = 1
            props["last_position"]["next_stop"]["sequence"] = 2
            features.append(handover)
        features.sort(key=lambda f: f["properties"]["trip"]["gtfs"]["trip_id"])
        polls.append({"features": features, "type": "FeatureCollection"})
    return polls


def _write(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def build(out: Path) -> list[Path]:
    """Write all synthetic fixtures and return their paths."""
    out.mkdir(parents=True, exist_ok=True)
    written = []
    polls = build_polls()
    for fetched_at, body in zip(POLL_TIMES, polls, strict=True):
        path = out / f"vehiclepositions_synthetic_{fetched_at:%Y%m%dT%H%M%SZ}.json"
        _write(path, body)
        written.append(path)

    empty = out / "vehiclepositions_empty.json"
    _write(empty, {"features": [], "type": "FeatureCollection"})
    written.append(empty)

    broken = json.loads(json.dumps(polls[0]["features"][0]))
    del broken["properties"]["last_position"]["origin_timestamp"]
    broken["properties"]["trip"]["gtfs"]["trip_id"] = 12345
    malformed = out / "vehiclepositions_malformed.json"
    _write(malformed, {"features": [broken], "type": "FeatureCollection"})
    written.append(malformed)
    return written


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Generate synthetic Golemio fixtures.")
    parser.add_argument("--out", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args()
    for path in build(args.out):
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
