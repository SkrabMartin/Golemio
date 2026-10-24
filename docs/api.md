# Golemio vehicle-positions API: observed contract

This page records what the Golemio API actually returned when probed, next to what its
official specification says. It is the reference for field names in Headway; the typed
contract is `src/headway/contracts/golemio_v1.py`. The test fixtures in
`tests/fixtures/golemio/` are synthetic: generated values with the same structure, null
patterns and edge cases as the real responses. Real samples are not redistributed here,
because the data's terms of use did not settle that.

- **Specification:** OpenAPI 3.0.3, "Public Transport | Golemio API", version 1.0.0,
  served at <https://api.golemio.cz/docs/static/vp-output-gateway/openapi.json> and rendered
  at <https://api.golemio.cz/pid/docs/openapi/>. Checked 2026-10-24.
- **Probe:** `scripts/probe_api.py`, three polls 30 s apart on Saturday 2026-10-24 around
  12:48 Europe/Prague. Null rates below come from this single midday sample (2,603 records)
  and will differ at night, at peak hours and on weekdays.

## Authentication and transport

- API key from <https://api.golemio.cz/api-keys>, sent in the `X-Access-Token` header.
- Responses are gzip-encoded JSON (`content-type: application/json; charset=utf-8`).
- Served through Cloudflare with `cache-control: public, s-maxage=5,
  stale-while-revalidate=5`, so the data can be up to ~10 s older than the cache entry.
- `x-last-modified` (ISO 8601, UTC) is documented and was present on every response.
- **No rate-limit headers** (`X-RateLimit-*`, `Retry-After`) were returned. The limits are
  therefore unknown; the collector must still honour `429` and `Retry-After` if they appear.
- Observed latency 210–380 ms.

## Endpoints

| Endpoint | Status in probe | Notes |
|---|---|---|
| `GET /v2/vehiclepositions` | 200 | Primary source. `limit` defaults to **100** and is capped at 10,000; `offset` pages. With `limit=10000`: 865–872 vehicles, ~1.0 MB raw, ~85 KB gzip per poll. |
| `GET /v2/public/vehiclepositions` | 200 | Reduced schema (8 fields), no paging parameters. 884–889 vehicles, ~230 KB raw, ~23 KB gzip. |
| `GET /v2/vehiclepositions/gtfsrt/vehicle_positions.pb` | 200 | Body not recorded. |
| `GET /v2/vehiclepositions/gtfsrt/trip_updates.pb` | 200 | Body not recorded. |
| `GET /v2/vehiclepositions/gtfsrt/alerts.pb` | 200 | Body not recorded. |
| `GET /v2/vehiclepositions/gtfsrt/pid_feed.pb` | 200 | Combined trip updates and positions. Body not recorded. |
| `GET /v2/vehiclepositions/gtfsrt/pid_display_alerts.pb` | 200 | Body not recorded. |

Documented but not probed query parameters of `/v2/vehiclepositions`: `includeNotTracking`,
`includeNotPublic` (default false), `cisTripNumber`, `preferredTimezone`, `routeId`,
`routeShortName`, `updatedSince`.

## `GET /v2/vehiclepositions` fields

The body is a GeoJSON object `{"type": "FeatureCollection", "features": [...]}`, with
top-level keys `type` and `features`; each feature is one vehicle on one trip.
Paths are relative to a feature. "Null %" is over all 2,603 probed records.

| Path | Type | Null % | Unit / format | Meaning |
|---|---|---|---|---|
| `type` | str | 0 | `"Feature"` | GeoJSON type |
| `geometry.type` | str | 0 | `"Point"` | |
| `geometry.coordinates` | [float, float] | 0 | WGS 84 `[lon, lat]` | Position from the same vehicle message as `origin_timestamp`. Spec: may be `[null, null]` before a trip is tracked (not observed). |
| **`properties.last_position`** | | | | |
| `.origin_timestamp` | str | 0 | ISO 8601 with offset | Time the vehicle sent the message. See discrepancy 1. |
| `.bearing` | int | 0 | degrees, 0 = north | Spec: nullable. |
| `.speed` | int \| null | 68.1 | km/h | |
| `.shape_dist_traveled` | str \| null | 0 | km, decimal string | Distance travelled along the trip shape. |
| `.state_position` | str | 0 | enum | Observed: `on_track`, `at_stop`. See discrepancy 2. |
| `.tracking` | bool | 0 | | Trip is on its track. Always `true` in the probe (not-tracking trips are excluded by default). |
| `.is_canceled` | bool \| null | 57.7 | | Trip canceled; other position fields undefined if true. Observed only `false` or null. |
| `.delay.actual` | int | 0 | seconds | Golemio's current delay estimate from the position between stops. Spec: nullable. |
| `.delay.last_stop_arrival` | int \| null | 6.2 | seconds | Delay on arrival at the last confirmed stop; null if arrival equals departure. |
| `.delay.last_stop_departure` | int \| null | 26.7 | seconds | Delay on departure from the last confirmed stop; null at a terminal stop. |
| `.last_stop.id` | str | 0 | GTFS `stop_id` | Last stop passed. |
| `.last_stop.sequence` | int | 0 | GTFS `stop_sequence` | |
| `.last_stop.arrival_time` | str | 0 | ISO 8601 with offset | Scheduled arrival at that stop. |
| `.last_stop.departure_time` | str | 0 | ISO 8601 with offset | Scheduled departure from that stop. |
| `.next_stop.id` / `.sequence` / `.arrival_time` / `.departure_time` | as above | 0 | as above | Same for the next stop. |
| **`properties.trip`** | | | | |
| `.gtfs.trip_id` | str | 0 | GTFS `trip_id` | Unique within one poll in the probe. |
| `.gtfs.route_id` | str | 0 | GTFS `route_id` | e.g. `L22` |
| `.gtfs.route_short_name` | str | 0 | | Public line name, e.g. `22`, `C` |
| `.gtfs.route_type` | int | 0 | GTFS route type | Observed 0 tram, 1 metro, 2 rail, 3 bus, 4 ferry, 11 trolleybus. |
| `.gtfs.trip_headsign` | str | 0 | | Destination shown on the vehicle. Spec: nullable. |
| `.gtfs.trip_short_name` | str \| null | 89.6 | | Train number such as `Os 8838`; null for other modes. |
| `.start_timestamp` | str | 0 | ISO 8601 with offset | Scheduled time at the first stop. |
| `.vehicle_registration_number` | int \| null | 14.4 | | Fleet number. See discrepancy 4. |
| `.vehicle_type` | object \| null | 10.4 | | `id` (int), `description_cs`, `description_en`; null for trains in the probe. |
| `.agency_name.scheduled` | str | 0 | | Operator name. See discrepancy 3. |
| `.agency_name.real` | str \| null | 10.4 | | Operator name. See discrepancy 3. |
| `.cis.line_id` | str \| null | 57.7 | | National CIS line number; the string `"none"` for trains. |
| `.cis.trip_number` | int \| null | 57.7 | | CIS trip number within the line. |
| `.origin_route_name` | str \| null | 10.4 | | Route the vehicle came from. |
| `.sequence_id` | int \| null | 10.4 | | Sequence of trips run by one vehicle. |
| `.wheelchair_accessible` | bool | 0 | | |
| `.air_conditioned` | bool \| null | 15.5 | | Null if unknown or registration number unknown. |
| `.usb_chargers` | bool \| null | 15.5 | | Null if unknown or registration number unknown. |

The CIS fields and `is_canceled` are null for exactly the same 1,503 records, all operated by
DP PRAHA (the city operator).

### Position age

`fetched_at − origin_timestamp` per record: median ~20 s, 90th percentile ~55 s,
99th percentile 160–190 s, maximum ~290 s.

## Discrepancies between specification and data

1. **Time zone.** The spec documents timestamps as UTC (`…Z`); the responses carry the
   Prague offset (`2026-10-24T12:48:37+02:00`). Both are valid ISO 8601 and resolve to the
   same instant, so the contract requires an offset and Headway converts to UTC. The
   `preferredTimezone` parameter may explain the default; not probed.
2. **`state_position`** is returned but not documented for this endpoint. The public
   endpoint documents it as an enum: `at_stop`, `before_track`, `before_track_delayed`,
   `canceled`, `off_track`, `on_track` (several further internal states are excluded from
   the API). The contract accepts exactly the documented values.
3. **`agency_name`.** The spec describes `real` as the agency that *normally* operates the
   trip and `scheduled` as the one that *currently* operates it, which reads as swapped.
   Which is which is unknown until checked against cases where they differ.
4. **Vehicle identity.** This endpoint has no vehicle id. `vehicle_registration_number`
   (described as a four-digit system id) is not unique: 8–10 numbers per poll appeared
   twice. Most were a DP PRAHA tram and a bus of another operator sharing a number. The
   public endpoint's `vehicle_id` has the form `service-<route_type>-<registration>`
   (744 of 744 matched trips agreed), `train-<number>` for trains and `metro-<line>-<n>` for
   metro, so `(route_type, vehicle_registration_number)` is the usable key here.
   Even with that key, one poll held 2 duplicates: the same bus on two consecutive trips
   (same `sequence_id`, adjacent trip numbers such as `103_251_…` and `103_252_…`) with
   identical `origin_timestamp` and coordinates: one `on_track` at stop sequence 8 or 11,
   the other `at_stop` at stop sequence 2 or 3.
   One vehicle message can therefore appear on two trips at a trip handover.
5. **Coverage.** The public endpoint returned ~15 more trips per poll, mostly in state
   `off_track`, which never appeared in this endpoint's output.

## `GET /v2/public/vehiclepositions` fields

GeoJSON `FeatureCollection`; `geometry` is a `Point`.

| Path | Type | Null % | Meaning |
|---|---|---|---|
| `properties.vehicle_id` | str | 0 | See discrepancy 4 |
| `properties.gtfs_trip_id` | str | 0 | GTFS `trip_id` |
| `properties.gtfs_route_short_name` | str | 0 | Public line name |
| `properties.route_type` | str | 0 | `bus`, `tram`, `train`, `metro`, `trolleybus`, `ferry` observed |
| `properties.delay` | int \| null | 0.3 | Delay; unit not documented (assumed seconds, unverified) |
| `properties.bearing` | int \| null | 0.6 | Degrees |
| `properties.state_position` | str | 0 | Observed `on_track`, `at_stop`, `off_track` |

It has **no timestamp of any kind**, so position age cannot be measured from it.

## Data licence

Golemio data is used under the operator's API terms of use. The static timetable is
published by ROPID under CC BY 4.0. This repository contains no raw Golemio responses; the
figures on this page are aggregates computed from the probe.
