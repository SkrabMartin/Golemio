"""The findings table: one row per problem found."""

from dataclasses import dataclass
from datetime import datetime

import pyarrow as pa

# category -> what it means, in plain words (used by the Markdown summary).
CATEGORIES: dict[str, str] = {
    "collection_gap": (
        "The collector got no successful response for much longer than the poll interval."
    ),
    "api_errors": "The API failed on every attempt of one or more polls in a row.",
    "stale_feed": "The API returned the identical response several times, so positions froze.",
    "stale_position": "A position reached the collector long after the vehicle sent it.",
    "gps_jump": "A vehicle moved between two reports faster than any vehicle of its type can.",
    "off_shape": "A position lies far from the route that the timetable gives for the trip.",
    "vehicle_on_two_trips": "One vehicle is reported on two trips at the same time.",
    "silent_trip": (
        "A trip sent no new position for minutes, or stopped short of the end of its route, "
        "with no collector problem to explain it."
    ),
    "unknown_trip": "The trip id is not in the timetable valid for that day.",
}

FINDINGS_SCHEMA = pa.schema(
    [
        pa.field("service_date", pa.date32(), nullable=False),
        pa.field("category", pa.string(), nullable=False),
        pa.field("severity", pa.string(), nullable=False),
        pa.field("entity_type", pa.string(), nullable=False),
        pa.field("entity", pa.string(), nullable=False),
        pa.field("start_ts", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("end_ts", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("value", pa.float64()),
        pa.field("details", pa.string(), nullable=False),
    ]
)


@dataclass(frozen=True)
class Finding:
    """One problem. ``value`` is the measured quantity (seconds, metres, m/s, a count)."""

    category: str
    entity_type: str  # poll | trip | vehicle
    entity: str
    start: datetime
    end: datetime
    value: float | None
    details: str
