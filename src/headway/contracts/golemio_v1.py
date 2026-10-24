"""Contract v1 for the Golemio ``GET /v2/vehiclepositions`` JSON response.

Field names, types and nullability follow the recorded responses in
``tests/fixtures/golemio/`` and the official OpenAPI specification; both are described in
``docs/api.md``. Where the two disagree, the recorded data wins and the difference is noted
on the field.

Validation is strict (no silent type coercion) and runs on the raw JSON bytes with
:func:`parse_feed`. Unknown fields are accepted and kept; :func:`extra_field_paths` lists
them so that ingest can report format changes.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

CONTRACT_VERSION = "golemio_v1"

# Documented values of ``state_position`` (OpenAPI enum of the public endpoint; the full
# endpoint returns the field without documenting it).
StatePosition = Literal[
    "at_stop",
    "before_track",
    "before_track_delayed",
    "canceled",
    "off_track",
    "on_track",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True, frozen=True)


class PointGeometry(_Model):
    """GeoJSON point; coordinates are ``[lon, lat]`` and may both be null per the spec."""

    type: Literal["Point"]
    coordinates: tuple[float | None, float | None]


class AgencyName(_Model):
    """Operator names. The spec's descriptions of the two fields appear swapped."""

    real: str | None
    scheduled: str


class Cis(_Model):
    """Identifiers from the national CIS timetable system.

    ``line_id`` is the string ``"none"`` (not null) for some trips, e.g. trains.
    """

    line_id: str | None
    trip_number: int | None


class TripGtfs(_Model):
    """GTFS static identifiers of the trip."""

    route_id: str
    route_short_name: str
    route_type: int
    trip_headsign: str | None
    trip_id: str
    trip_short_name: str | None


class VehicleType(_Model):
    """Vehicle type code and descriptions."""

    id: int
    description_cs: str
    description_en: str


class Trip(_Model):
    """Static and vehicle information about the trip."""

    agency_name: AgencyName
    air_conditioned: bool | None
    cis: Cis
    gtfs: TripGtfs
    origin_route_name: str | None
    sequence_id: int | None
    start_timestamp: AwareDatetime
    usb_chargers: bool | None
    vehicle_registration_number: int | None = Field(
        description="Fleet number; unique only together with the route type, not globally."
    )
    vehicle_type: VehicleType | None
    wheelchair_accessible: bool


class Delay(_Model):
    """Delays in seconds, as computed by Golemio."""

    actual: int | None
    last_stop_arrival: int | None
    last_stop_departure: int | None


class StopRef(_Model):
    """Last passed or next stop with its scheduled times."""

    id: str | None
    sequence: int | None
    arrival_time: AwareDatetime | None
    departure_time: AwareDatetime | None


class LastPosition(_Model):
    """The most recent position of the vehicle on this trip."""

    bearing: int | None
    delay: Delay
    is_canceled: bool | None
    last_stop: StopRef
    next_stop: StopRef
    origin_timestamp: AwareDatetime
    shape_dist_traveled: str | None = Field(
        description="Kilometres along the shape, encoded as a decimal string."
    )
    speed: int | None
    state_position: StatePosition
    tracking: bool

    @field_validator("shape_dist_traveled")
    @classmethod
    def _decimal_string(cls, value: str | None) -> str | None:
        if value is not None:
            float(value)  # raises ValueError, reported as a validation error
        return value


class VehicleProperties(_Model):
    """Properties of one vehicle feature."""

    trip: Trip
    last_position: LastPosition


class VehicleFeature(_Model):
    """One vehicle on one trip."""

    type: Literal["Feature"]
    geometry: PointGeometry
    properties: VehicleProperties


class VehiclePositionsFeed(_Model):
    """Full response of ``GET /v2/vehiclepositions``."""

    type: Literal["FeatureCollection"]
    features: list[VehicleFeature]


def parse_feed(raw: bytes | str) -> VehiclePositionsFeed:
    """Validate a raw response body; raises :class:`pydantic.ValidationError` on mismatch."""
    return VehiclePositionsFeed.model_validate_json(raw)


def extra_field_paths(model: BaseModel, prefix: str = "") -> set[str]:
    """Return dotted paths of fields present in the data but not in the contract.

    List elements are collapsed to ``[]`` so each unknown field is reported once.
    """
    paths: set[str] = set()
    for name in model.model_extra or {}:
        paths.add(f"{prefix}{name}")
    for name in type(model).model_fields:
        value = getattr(model, name)
        items = value if isinstance(value, list | tuple) else [value]
        suffix = "[]." if isinstance(value, list | tuple) else "."
        for item in items:
            if isinstance(item, BaseModel):
                paths |= extra_field_paths(item, f"{prefix}{name}{suffix}")
    return paths
