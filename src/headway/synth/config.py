"""Typed parameters of the synthetic archive generator (``config/synth.toml``)."""

import tomllib
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_SYNTH_CONFIG = Path("config/synth.toml")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MeanSd(_Section):
    """A normal distribution."""

    mean: float
    sd: float = Field(ge=0)


class NetworkConfig(_Section):
    """Shape of the synthetic network."""

    tram_routes: int = Field(ge=1)
    bus_routes: int = Field(ge=0)
    regional_bus_share: float = Field(ge=0, le=1)
    stops_per_route: tuple[int, int]
    stop_spacing_m: tuple[float, float]
    shape_step_m: float = Field(gt=0)
    max_turn_deg: float = Field(ge=0)
    origin_lat: float
    origin_lon: float
    radius_m: float = Field(gt=0)


class TimetableConfig(_Section):
    """How the timetable is built."""

    first_departure: str
    last_departure: str
    headway_min: dict[str, int]
    peak_hours: list[tuple[int, int]]
    evening_from_hour: int
    night_hours: tuple[int, int]
    speed_kmh: dict[str, float]
    timing_point_every: int = Field(ge=2)
    padded_segments_per_route: int = Field(ge=0)
    layover_min: int = Field(ge=0)
    timetable_change_day: int = Field(ge=0)
    timetable_change_shift_min: int

    @model_validator(mode="after")
    def _keys(self) -> "TimetableConfig":
        if set(self.headway_min) != {"peak", "day", "evening", "weekend", "night"}:
            raise ValueError("headway_min needs peak, day, evening, weekend and night")
        if set(self.speed_kmh) != {"tram", "bus"}:
            raise ValueError("speed_kmh needs tram and bus")
        return self


class DelayConfig(_Section):
    """The true delay process."""

    initial_delay_s: MeanSd
    segment_increment_s: MeanSd
    peak_increment_factor: float = Field(ge=0)
    bottlenecks_per_route: int = Field(ge=0)
    bottleneck_increment_s: float
    dwell_s: MeanSd
    min_turnaround_s: float = Field(ge=0)


class ReportingConfig(_Section):
    """How vehicles report and how the API publishes."""

    report_interval_s: tuple[float, float]
    publish_lag_s: float = Field(ge=0)
    long_lag_probability: float = Field(ge=0, le=1)
    long_lag_s: tuple[float, float]
    linger_s: float = Field(ge=0)
    drop_after_s: float = Field(gt=0)
    gps_noise_m: float = Field(ge=0)
    golemio_delay_noise_s: float = Field(ge=0)
    golemio_delay_bias_s: float
    regional_delay_rounding_s: int = Field(ge=1)


class DefectConfig(_Section):
    """Planted data-quality problems (counts are Poisson means per day)."""

    collection_gaps: float = Field(ge=0)
    collection_gap_min: tuple[float, float]
    api_errors: float = Field(ge=0)
    stale_episodes: float = Field(ge=0)
    stale_polls: tuple[int, int]
    gps_jumps: float = Field(ge=0)
    gps_jump_m: tuple[float, float]
    off_shape: float = Field(ge=0)
    off_shape_m: tuple[float, float]
    off_shape_min: tuple[float, float]
    silent_trips: float = Field(ge=0)
    silent_min: tuple[float, float]
    handover_duplicate_probability: float = Field(ge=0, le=1)


class SynthConfig(_Section):
    """All generator parameters."""

    seed: int
    start_date: date
    days: int = Field(ge=1)
    poll_interval_s: int = Field(ge=1)
    output_dir: Path
    network: NetworkConfig
    timetable: TimetableConfig
    delay: DelayConfig
    reporting: ReportingConfig
    defects: DefectConfig


def load_synth_config(path: Path = DEFAULT_SYNTH_CONFIG) -> SynthConfig:
    """Load the generator parameters from TOML."""
    with path.open("rb") as f:
        return SynthConfig.model_validate(tomllib.load(f))
