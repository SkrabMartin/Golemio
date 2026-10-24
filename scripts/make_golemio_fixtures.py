"""Build local real-data fixtures from a probe run, deterministically.

Reads the raw polls written by ``scripts/probe_api.py`` (default ``probe_out/run2``) and
writes to ``tests/fixtures/golemio_real/``, which is git-ignored: real responses are not
redistributed (the committed fixtures are synthetic, see
``scripts/make_synthetic_golemio_fixtures.py``). Tests that need these files skip when
they are absent. Files:

- ``vehiclepositions_<fetched_at>.json``: one file per poll, trimmed to the same set of at
  most ``--max-vehicles`` trips in every poll, so that later phases can test behaviour
  across consecutive polls. Trips are chosen greedily to cover as many field variants
  (JSON types, null patterns, route types, position states) as possible, then filled up
  in trip-id order.
- ``vehiclepositions_empty.json``: synthetic, a feed with no vehicles.
- ``vehiclepositions_malformed.json``: synthetic, one record with a missing
  ``origin_timestamp`` and a ``trip_id`` of the wrong type.

The response bodies are kept as they are apart from the trimming; only the features list is
shortened. Re-running on the same probe output produces identical files.

Usage::

    uv run python scripts/make_golemio_fixtures.py --probe probe_out/run2
"""

import argparse
import copy
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

FIXTURE_DIR = Path("tests/fixtures/golemio_real")
POLL_NAME = re.compile(r"poll_\d+_(\d{8}T\d{6}Z)\.body$")


def _signature(value: Any, path: str = "") -> Iterator[str]:
    """Yield ``path:type`` items describing the shape of a JSON value."""
    if isinstance(value, dict):
        yield f"{path}:object"
        for key, child in value.items():
            yield from _signature(child, f"{path}.{key}" if path else key)
    elif value is None:
        yield f"{path}:null"
    else:
        yield f"{path}:{type(value).__name__}"


def _trip_id(feature: dict[str, Any]) -> str:
    return str(feature["properties"]["trip"]["gtfs"]["trip_id"])


def _coverage(feature: dict[str, Any]) -> set[str]:
    props = feature["properties"]
    items = set(_signature(props))
    items.add(f"route_type={props['trip']['gtfs']['route_type']}")
    items.add(f"state_position={props['last_position']['state_position']}")
    return items


def select_trips(polls: list[list[dict[str, Any]]], max_vehicles: int) -> list[str]:
    """Choose up to ``max_vehicles`` trip ids present in every poll."""
    common = set.intersection(*({_trip_id(f) for f in poll} for poll in polls))
    first = {_trip_id(f): f for f in polls[0] if _trip_id(f) in common}

    # Keep pairs of trips that share a registration number, a case the audit must handle.
    by_reg: dict[int, list[str]] = {}
    for trip, feature in sorted(first.items()):
        reg = feature["properties"]["trip"]["vehicle_registration_number"]
        if reg is not None:
            by_reg.setdefault(reg, []).append(trip)
    chosen: list[str] = []
    for trips in by_reg.values():
        if len(trips) > 1 and len(chosen) + 2 <= max_vehicles:
            chosen.extend(trips[:2])
            break

    covered: set[str] = set().union(*(_coverage(first[t]) for t in chosen))
    remaining = sorted(set(first) - set(chosen))
    while len(chosen) < max_vehicles and remaining:
        best = max(remaining, key=lambda t: (len(_coverage(first[t]) - covered), t))
        gain = _coverage(first[best]) - covered
        if not gain:
            break
        chosen.append(best)
        covered |= gain
        remaining.remove(best)
    chosen.extend(remaining[: max_vehicles - len(chosen)])
    return sorted(chosen)


def _write(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def build(probe: Path, out: Path, max_vehicles: int) -> list[Path]:
    """Write all fixtures and return their paths."""
    bodies = sorted(p for p in probe.glob("poll_*.body") if POLL_NAME.search(p.name))
    if not bodies:
        raise SystemExit(f"no poll_*.body files in {probe}")
    raw = [json.loads(p.read_bytes()) for p in bodies]
    keep = set(select_trips([body["features"] for body in raw], max_vehicles))

    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for path, body in zip(bodies, raw, strict=True):
        match = POLL_NAME.search(path.name)
        assert match is not None
        trimmed = dict(body)
        trimmed["features"] = sorted(
            (f for f in body["features"] if _trip_id(f) in keep), key=_trip_id
        )
        target = out / f"vehiclepositions_{match.group(1)}.json"
        _write(target, trimmed)
        written.append(target)

    empty = out / "vehiclepositions_empty.json"
    _write(empty, {"features": [], "type": "FeatureCollection"})
    written.append(empty)

    malformed_feature = copy.deepcopy(raw[0]["features"][0])
    del malformed_feature["properties"]["last_position"]["origin_timestamp"]
    malformed_feature["properties"]["trip"]["gtfs"]["trip_id"] = 12345
    malformed = out / "vehiclepositions_malformed.json"
    _write(malformed, {"features": [malformed_feature], "type": "FeatureCollection"})
    written.append(malformed)
    return written


def main() -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Build Golemio fixtures from a probe run.")
    parser.add_argument("--probe", type=Path, default=Path("probe_out/run2"))
    parser.add_argument("--out", type=Path, default=FIXTURE_DIR)
    parser.add_argument("--max-vehicles", type=int, default=50)
    args = parser.parse_args()
    for path in build(args.probe, args.out, args.max_vehicles):
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
