import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from headway.contracts.golemio_v1 import extra_field_paths, parse_feed

FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC = sorted((FIXTURES / "golemio").glob("vehiclepositions_synthetic_*.json"))
# Real probe samples are not committed; these tests run only where they were built locally
# with scripts/make_golemio_fixtures.py.
REAL = sorted((FIXTURES / "golemio_real").glob("vehiclepositions_2*.json"))
needs_real = pytest.mark.skipif(not REAL, reason="real samples not present (not committed)")


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for child in value.values() for k in _keys(child)}
    if isinstance(value, list):
        return {k for child in value for k in _keys(child)}
    return set()


def _load(paths: list[Path]) -> list[Any]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def test_synthetic_fixtures_exist() -> None:
    assert len(SYNTHETIC) == 3


@pytest.mark.parametrize("path", SYNTHETIC + REAL, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_fixture_validates_without_unknown_fields(path: Path) -> None:
    feed = parse_feed(path.read_bytes())
    assert 0 < len(feed.features) <= 50
    assert extra_field_paths(feed) == set()


@pytest.mark.parametrize("path", SYNTHETIC + REAL, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_timestamps_are_timezone_aware(path: Path) -> None:
    feed = parse_feed(path.read_bytes())
    for feature in feed.features:
        assert feature.properties.last_position.origin_timestamp.utcoffset() is not None


@needs_real
def test_synthetic_fixtures_cover_every_real_field() -> None:
    real_keys = set().union(*(_keys(body) for body in _load(REAL)))
    synthetic_keys = set().union(*(_keys(body) for body in _load(SYNTHETIC)))
    assert real_keys - synthetic_keys == set()


def test_synthetic_fixtures_cover_edge_cases() -> None:
    feeds = [parse_feed(p.read_bytes()) for p in SYNTHETIC]
    features = [f for feed in feeds for f in feed.features]
    trips = {f.properties.trip for f in features}
    assert {t.gtfs.route_type for t in trips} == {0, 1, 2, 3, 4, 11}
    assert {f.properties.last_position.state_position for f in features} == {
        "at_stop",
        "on_track",
        "off_track",
    }
    assert any(t.vehicle_registration_number is None for t in trips)
    assert any(f.properties.last_position.speed is None for f in features)

    first = feeds[0].features
    by_reg: dict[int, set[int]] = {}
    by_message: dict[tuple[int, int, str], int] = {}
    for f in first:
        reg = f.properties.trip.vehicle_registration_number
        if reg is None:
            continue
        by_reg.setdefault(reg, set()).add(f.properties.trip.gtfs.route_type)
        key = (
            f.properties.trip.gtfs.route_type,
            reg,
            str(f.properties.last_position.origin_timestamp),
        )
        by_message[key] = by_message.get(key, 0) + 1
    assert any(len(types) > 1 for types in by_reg.values()), "shared registration number"
    assert any(n > 1 for n in by_message.values()), "trip handover duplicate"


def test_api_doc_mentions_every_fixture_field() -> None:
    doc = (Path(__file__).parents[1] / "docs" / "api.md").read_text(encoding="utf-8")
    documented = {part for token in re.findall(r"`([^`]+)`", doc) for part in token.split(".")}
    keys = set().union(*(_keys(body) for body in _load(SYNTHETIC + REAL)))
    assert keys - documented == set()


def test_empty_feed() -> None:
    feed = parse_feed((FIXTURES / "golemio" / "vehiclepositions_empty.json").read_bytes())
    assert feed.features == []


def test_malformed_fixture_gives_clear_error() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse_feed((FIXTURES / "golemio" / "vehiclepositions_malformed.json").read_bytes())
    locations = {tuple(err["loc"]) for err in excinfo.value.errors()}
    assert ("features", 0, "properties", "last_position", "origin_timestamp") in locations
    assert ("features", 0, "properties", "trip", "gtfs", "trip_id") in locations


def _one_feature() -> dict[str, Any]:
    body = json.loads(SYNTHETIC[0].read_text(encoding="utf-8"))
    return {"type": "FeatureCollection", "features": [body["features"][0]]}


def test_unknown_field_is_accepted_and_recorded() -> None:
    body = _one_feature()
    body["features"][0]["properties"]["last_position"]["new_field"] = 1
    body["generated_at"] = "x"
    feed = parse_feed(json.dumps(body))
    assert extra_field_paths(feed) == {
        "generated_at",
        "features[].properties.last_position.new_field",
    }


def test_naive_timestamp_is_rejected() -> None:
    body = _one_feature()
    body["features"][0]["properties"]["last_position"]["origin_timestamp"] = "2026-10-24T12:48:37"
    with pytest.raises(ValidationError):
        parse_feed(json.dumps(body))


def test_numeric_string_is_not_coerced_to_int() -> None:
    body = _one_feature()
    body["features"][0]["properties"]["last_position"]["speed"] = "30"
    with pytest.raises(ValidationError):
        parse_feed(json.dumps(body))


def test_unknown_state_position_is_rejected() -> None:
    body = _one_feature()
    body["features"][0]["properties"]["last_position"]["state_position"] = "teleported"
    with pytest.raises(ValidationError):
        parse_feed(json.dumps(body))
