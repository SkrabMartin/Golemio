"""Arrow schemas of the event outputs (the file contract of layer L3)."""

import pyarrow as pa

_TS = pa.timestamp("us", tz="UTC")

EVENTS_SCHEMA = pa.schema(
    [
        pa.field("service_date", pa.date32(), nullable=False),
        pa.field("trip_id", pa.string(), nullable=False),
        pa.field("trip_start_ts", _TS, nullable=False),
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("route_type", pa.int16(), nullable=False),
        pa.field("direction_id", pa.int8()),
        pa.field("vehicle_id", pa.string()),
        pa.field("stop_id", pa.string(), nullable=False),
        pa.field("stop_sequence", pa.int32(), nullable=False),
        pa.field("stop_dist_m", pa.float64(), nullable=False),
        pa.field("arr_ts", _TS),
        pa.field("dep_ts", _TS),
        pa.field("sched_arr_ts", _TS),
        pa.field("sched_dep_ts", _TS),
        pa.field("arr_delay_s", pa.float64()),
        pa.field("dep_delay_s", pa.float64()),
        pa.field("delay_s", pa.float64()),
        pa.field("uncert_s", pa.float64()),
        pa.field("dep_uncert_s", pa.float64()),
        pa.field("n_positions", pa.int32(), nullable=False),
        pa.field("method", pa.string(), nullable=False),
        pa.field("gtfs_sha256", pa.string()),
    ]
)

EXCLUSIONS_SCHEMA = pa.schema(
    [
        pa.field("service_date", pa.date32(), nullable=False),
        pa.field("trip_id", pa.string(), nullable=False),
        pa.field("trip_start_ts", _TS, nullable=False),
        pa.field("reason", pa.string(), nullable=False),
        pa.field("detail", pa.string(), nullable=False),
    ]
)

COMPARE_SCHEMA = pa.schema(
    [
        pa.field("service_date", pa.date32(), nullable=False),
        pa.field("trip_id", pa.string(), nullable=False),
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("route_type", pa.int16(), nullable=False),
        pa.field("stop_sequence", pa.int32(), nullable=False),
        pa.field("stop_position", pa.string(), nullable=False),
        pa.field("event_ts", _TS, nullable=False),
        pa.field("reconstructed_delay_s", pa.float64(), nullable=False),
        pa.field("reported_delay_s", pa.float64(), nullable=False),
        pa.field("diff_s", pa.float64(), nullable=False),
        pa.field("dt_s", pa.float64(), nullable=False),
        pa.field("uncert_s", pa.float64()),
    ]
)
