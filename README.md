# Headway

A reliability lab for Prague public transport (PID).

Prague publishes live vehicle positions through the [Golemio](https://golemio.cz) data
platform, but only the present moment: there is no public archive to ask how reliable a
tram line was last Tuesday morning. Headway is a pipeline that builds that history and
studies it:

- **Collect** raw vehicle-position snapshots unmodified, with checksums and a manifest.
- **Reconstruct** stop-level arrival and departure times from positions, with an
  uncertainty estimate for every event.
- **Audit** the data source: collection gaps, stale positions, GPS jumps, mismatched
  trips, and how the published delay compares with an independent reconstruction.
- **Measure** reliability (on-time share, headway regularity, excess waiting time,
  bunching) and where along a route delay is added or absorbed.
- **Nowcast** downstream delays and transfer-miss risk with calibrated intervals,
  evaluated once on a holdout that is locked before any model is fitted, against
  simple, honest baselines.

Headway is a historical research project, not a journey planner or a live service.

## Real data and synthetic data

Headway is a **proof of concept**. The collector is fully implemented, but it is not
running anywhere, because a meaningful archive needs a machine that polls the API
around the clock for weeks.

- **What is real:** the behaviour of the Golemio API. It was probed directly, and every
  field, null rate and discrepancy between the API's specification and its responses is
  documented in [docs/api.md](docs/api.md). The data contract in
  `src/headway/contracts/golemio_v1.py` is written against those real responses.
- **What is synthetic:** the archive the analysis runs on. `headway synth` produces raw
  snapshots in exactly the collector's file format, from a simulated network whose true
  delays, gaps and data defects are known. Because the truth is known, every stage can be
  checked: does the audit find the planted defects, does the reconstruction recover the
  true arrival times, do the prediction intervals cover what they claim?

Results computed from synthetic data show that the methods work. They are not findings
about Prague, and they are labelled as synthetic wherever they appear. Point the same
pipeline at a real archive (see [Collecting real data](#collecting-real-data)) and it
produces real results.

## Status

| Layer | What it does | State |
|---|---|---|
| L0 collect | Polls the API, stores raw snapshots and a manifest | Implemented and tested |
| GTFS | Daily timetable snapshots, version per service date, time conversion | Implemented and tested |
| Synthetic archive | Generates raw snapshots with known ground truth | Implemented and tested |
| L1 ingest | Raw snapshots to a partitioned positions dataset, with rejects and a daily report | Implemented and tested |
| L2 audit | Data-quality findings | Planned |
| L3 events | Stop arrival and departure reconstruction | Planned |
| L4 metrics | Reliability and delay propagation | Planned |
| L5 model | Split, baselines, nowcasts, conformal intervals | Planned |
| L6 export | Static web map, Streamlit app, report | Planned |

## Quick start

Requires [uv](https://docs.astral.sh/uv/). No API key is needed.

```sh
uv sync
uv run headway --help
```

Development checks (the same ones CI runs):

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest
```

Tests run fully offline and never call the Golemio API.

## Generating the synthetic archive

```sh
uv run headway synth --days 1        # one synthetic day, about a minute, ~12 MB
uv run headway synth                 # the full 6 weeks from config/synth.toml
```

The output goes to `synthetic/` (git-ignored):

```
synthetic/
  SYNTHETIC.json                  marker with parameters and counts
  raw/YYYY/MM/DD/*.json.gz        API responses in the collector's format
  raw/manifest/YYYY-MM-DD.parquet one row per request, as the collector writes it
  gtfs/                           timetable versions and their registry
  truth/stop_events.parquet       true arrival and departure at every stop
  truth/segments.parquet          planted bottlenecks and schedule padding
  truth/defects.parquet           every planted data defect, with time and trip
```

The simulated network has 3 tram and 3 bus routes around central Prague, a weekday and a
weekend timetable with peak, evening and night headways, and vehicle blocks that carry
delay from one trip to the next. Vehicles report every 5 to 30 seconds with a publication
lag tuned to the position ages seen in the real probe. The generator plants collection
gaps, API errors, stale feeds, GPS jumps, positions off the route, silent vehicles,
duplicate records at trip handovers and a systematic bias in the API's own delay
estimate on one route. Every parameter is in `config/synth.toml`, and the same seed
always produces identical output.

## Building the positions dataset

```sh
uv run headway ingest --all --archive synthetic   # every service date of the synthetic archive
uv run headway ingest --date 2026-10-24           # one date of a real archive (./raw, ./gtfs)
```

Ingest turns the raw polls into one row per vehicle position report. Every service date is
rebuilt from the raw files, so running it again gives the same output and never duplicates.
Results go next to the archive they came from, so synthetic and real data never mix:

```
<archive>/data/
  ORIGIN.json                                       says whether the archive is synthetic
  positions/service_date=YYYY-MM-DD/part-0.parquet  one row per position report
  rejects/service_date=YYYY-MM-DD/part-0.parquet    records that failed validation, with a reason
  ingest_reports/YYYY-MM-DD.json                    counts, rejects by reason, speed and memory
```

- A trip belongs to the service day in which it starts. A service day begins at 04:30 local
  time (a Headway convention, set in `config/default.toml`), so trips after midnight stay
  with the previous day.
- A report that appears in several polls (same trip, same origin time) is written once and
  keeps the first file it came from; `seen_in_polls` counts the repeats.
- The vehicle key is `<route type>-<registration number>`. Rail and metro have no
  registration number in this endpoint, so their `vehicle_id` is empty. When one vehicle
  message appears on two trips (a trip handover), both rows are kept and flagged
  `handover_duplicate`.
- Nothing is dropped silently: a bad feature or poll goes to `rejects/` with its reason.
  Polls holding exactly as many vehicles as the API `limit` are flagged (`poll_at_limit`),
  because vehicles may be missing from them.
- Each row names the GTFS version that applies to its service date.

Ingest holds one service date in memory. That is fine for the synthetic archive (about
440 MB at its peak for one synthetic day) but real days are roughly 27 times larger in
stored size, so a real archive would need a streaming version first.

## Collecting real data

To run Headway on real data you need a free Golemio API key from
<https://api.golemio.cz/api-keys>. The key is read only from the `GOLEMIO_TOKEN`
environment variable and is never written to disk.

**Try a single poll** (any machine):

```sh
export GOLEMIO_TOKEN=...          # PowerShell: $env:GOLEMIO_TOKEN = "..."
uv run headway collect --once     # writes raw/YYYY/MM/DD/*.json.gz and raw/manifest/
uv run headway snapshot-gtfs      # downloads the current timetable into gtfs/
```

**Build an archive** by running `uv run headway collect` continuously. It polls every
20 seconds, aligned to the clock, retries failures and stops cleanly on `Ctrl+C` or
`SIGTERM`. Plan for about **370 MB of compressed data per day**. Analyses need several
weeks of uninterrupted data, so use an always-on machine: [deploy/README.md](deploy/README.md)
is a step-by-step guide for a Raspberry Pi or any Linux server. It covers a hardened
systemd service, the daily timetable snapshot, nightly backups, disk-usage alerts and
optional phone notifications.

## Configuration

Defaults live in `config/default.toml`. Any value can be overridden with an environment
variable such as `HEADWAY_COLLECT__INTERVAL_S=30`.

## Data sources

- Vehicle positions: [Golemio API](https://api.golemio.cz/pid/docs/openapi/), operated by
  Operátor ICT, a.s.
- Static timetable: [PID GTFS](https://pid.cz/o-systemu/opendata/) published by ROPID
  under CC BY 4.0.

No raw Golemio data is redistributed in this repository. Test fixtures are synthetic and
mirror the structure of real responses.

## License

[MIT](LICENSE)
