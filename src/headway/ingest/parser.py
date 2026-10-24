"""Parser ``v1``: one raw response body to position rows and rejects.

The field mapping follows ``docs/api.md``. A body is validated against the
``golemio_v1`` contract; if the whole body fails, it is retried feature by feature so one
bad vehicle costs one reject instead of the whole poll.
"""

import json
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from pydantic import ValidationError

from headway.contracts.golemio_v1 import (
    VehicleFeature,
    VehiclePositionsFeed,
    extra_field_paths,
    parse_feed,
)

DETAIL_MAX_CHARS = 300


@dataclass(frozen=True)
class FeatureReject:
    """A feature (or whole body, ``feature_index`` None) that failed validation."""

    feature_index: int | None
    reason: str
    detail: str


@dataclass(frozen=True)
class ParsedPoll:
    """Result of parsing one body: rows, rejects, feature count and unknown field paths."""

    rows: list[dict[str, Any]]
    rejects: list[FeatureReject]
    feature_count: int
    extra_fields: set[str]


def vehicle_key(route_type: int, registration: int | None) -> str | None:
    """Return the vehicle key ``<route_type>-<registration>``, or None without a number.

    The endpoint has no vehicle id and the registration number alone is not unique across
    route types (docs/api.md, discrepancy 4).
    """
    return None if registration is None else f"{route_type}-{registration}"


def feature_to_row(feature: VehicleFeature) -> dict[str, Any]:
    """Map one validated feature to a position row (times converted to UTC)."""
    props = feature.properties
    trip = props.trip
    pos = props.last_position
    lon, lat = feature.geometry.coordinates
    return {
        "vehicle_id": vehicle_key(trip.gtfs.route_type, trip.vehicle_registration_number),
        "route_type": trip.gtfs.route_type,
        "trip_id": trip.gtfs.trip_id,
        "trip_start_ts": trip.start_timestamp.astimezone(UTC),
        "route_id": trip.gtfs.route_id,
        "route_short_name": trip.gtfs.route_short_name,
        "lat": lat,
        "lon": lon,
        "bearing": pos.bearing,
        "speed": pos.speed,
        "origin_ts": pos.origin_timestamp.astimezone(UTC),
        "reported_delay_s": pos.delay.actual,
        "last_stop_id": pos.last_stop.id,
        "last_stop_sequence": pos.last_stop.sequence,
        "next_stop_id": pos.next_stop.id,
        "next_stop_sequence": pos.next_stop.sequence,
        "state_position": pos.state_position,
        "shape_dist_km": None
        if pos.shape_dist_traveled is None
        else float(pos.shape_dist_traveled),
        "tracking": pos.tracking,
        "is_canceled": pos.is_canceled,
    }


def _detail(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{where}: {first['msg']}"[:DETAIL_MAX_CHARS]


def _rows_of(feed: VehiclePositionsFeed) -> list[dict[str, Any]]:
    return [feature_to_row(f) for f in feed.features]


def parse_poll(body: bytes, *, check_schema: bool = False) -> ParsedPoll:
    """Parse one raw body. ``check_schema`` also collects fields unknown to the contract."""
    try:
        feed = parse_feed(body)
    except ValidationError:
        return _parse_leniently(body)
    extra = extra_field_paths(feed) if check_schema else set()
    return ParsedPoll(_rows_of(feed), [], len(feed.features), extra)


def _parse_leniently(body: bytes) -> ParsedPoll:
    try:
        data = json.loads(body)
    except ValueError as exc:
        return ParsedPoll(
            [], [FeatureReject(None, "invalid_json", str(exc)[:DETAIL_MAX_CHARS])], 0, set()
        )
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        features = None
    if not isinstance(features, list):
        return ParsedPoll(
            [], [FeatureReject(None, "invalid_feed", "not a FeatureCollection")], 0, set()
        )
    rows: list[dict[str, Any]] = []
    rejects: list[FeatureReject] = []
    for index, raw in enumerate(features):
        try:
            feature = VehicleFeature.model_validate_json(json.dumps(raw))
        except ValidationError as exc:
            rejects.append(FeatureReject(index, "invalid_feature", _detail(exc)))
        else:
            rows.append(feature_to_row(feature))
    return ParsedPoll(rows, rejects, len(features), set())
