"""Arrow schemas of the ingest outputs (the file contract of layer L1).

``vehicle_id`` is ``<route_type>-<registration number>``: the endpoint has no vehicle id
and the registration number alone is not unique (docs/api.md, discrepancy 4).
"""

import pyarrow as pa

_TS = pa.timestamp("us", tz="UTC")

POSITIONS_SCHEMA = pa.schema(
    [
        pa.field("service_date", pa.date32(), nullable=False),
        pa.field("vehicle_id", pa.string()),
        pa.field("route_type", pa.int16(), nullable=False),
        pa.field("trip_id", pa.string(), nullable=False),
        pa.field("trip_start_ts", _TS, nullable=False),
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("route_short_name", pa.string(), nullable=False),
        pa.field("lat", pa.float64()),
        pa.field("lon", pa.float64()),
        pa.field("bearing", pa.int16()),
        pa.field("speed", pa.int16()),
        pa.field("origin_ts", _TS, nullable=False),
        pa.field("fetched_at", _TS, nullable=False),
        pa.field("reported_delay_s", pa.int32()),
        pa.field("last_stop_id", pa.string()),
        pa.field("last_stop_sequence", pa.int32()),
        pa.field("next_stop_id", pa.string()),
        pa.field("next_stop_sequence", pa.int32()),
        pa.field("state_position", pa.string(), nullable=False),
        pa.field("shape_dist_km", pa.float64()),
        pa.field("tracking", pa.bool_(), nullable=False),
        pa.field("is_canceled", pa.bool_()),
        pa.field("seen_in_polls", pa.int32(), nullable=False),
        pa.field("handover_duplicate", pa.bool_(), nullable=False),
        pa.field("poll_at_limit", pa.bool_(), nullable=False),
        pa.field("gtfs_snapshot_date", pa.date32()),
        pa.field("gtfs_sha256", pa.string()),
        pa.field("parser_version", pa.string(), nullable=False),
        pa.field("raw_sha256", pa.string(), nullable=False),
        pa.field("raw_path", pa.string(), nullable=False),
    ]
)

REJECTS_SCHEMA = pa.schema(
    [
        pa.field("service_date", pa.date32(), nullable=False),
        pa.field("fetched_at", _TS, nullable=False),
        pa.field("raw_path", pa.string()),
        pa.field("raw_sha256", pa.string()),
        pa.field("feature_index", pa.int32()),
        pa.field("reason", pa.string(), nullable=False),
        pa.field("detail", pa.string(), nullable=False),
    ]
)
