# Headway

A reliability lab for Prague public transport (PID).

Prague publishes live vehicle positions through the [Golemio](https://golemio.cz) data
platform, but only the present moment: there is no public archive to ask how reliable a
tram line was last Tuesday morning. Headway builds that history itself and studies it:

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

## Status

Early development. The project skeleton, configuration and quality gates are in place;
pipeline stages are added layer by layer.

## Quick start

Requires [uv](https://docs.astral.sh/uv/).

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

## Configuration

Defaults live in `config/default.toml`. Any value can be overridden with an environment
variable such as `HEADWAY_COLLECT__INTERVAL_S=30`. A Golemio API key is only needed for
collecting data and is read exclusively from the `GOLEMIO_TOKEN` environment variable.

## Data sources

- Vehicle positions: [Golemio API](https://api.golemio.cz/pid/docs/openapi/), operated by
  Operátor ICT, a.s.
- Static timetable: [PID GTFS](https://pid.cz/o-systemu/opendata/) published by ROPID
  under CC BY 4.0.

Raw data is not redistributed in this repository.

## License

[MIT](LICENSE)
