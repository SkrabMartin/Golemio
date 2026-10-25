"""Parameters of the stop-event reconstruction, loaded from ``config/events.toml``."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_EVENTS_CONFIG_PATH = Path("config/events.toml")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectionConfig(_Section):
    """How positions are projected onto the trip shape."""

    window_m: float = Field(gt=0, description="Search window around the API distance.")
    max_offset_m: float = Field(gt=0, description="Farther from the shape: position unused.")


class StopsConfig(_Section):
    """How stops are placed and matched."""

    radius_m: float = Field(gt=0, description="A standing vehicle this close serves the stop.")
    projected_stop_offset_m: float = Field(gt=0, description="Limit when projecting stops.")


class PlateauConfig(_Section):
    """What counts as a vehicle standing still."""

    eps_m: float = Field(gt=0, description="Positions this close are one place.")


class TripConfig(_Section):
    """Requirements on a whole trip."""

    min_positions: int = Field(ge=2, description="Usable positions needed.")


class CompareConfig(_Section):
    """Comparison with the delay the API reports."""

    max_dt_s: float = Field(gt=0, description="Largest time gap to the compared position.")


class EventsConfig(_Section):
    """All reconstruction parameters."""

    projection: ProjectionConfig
    stops: StopsConfig
    plateau: PlateauConfig
    trip: TripConfig
    compare: CompareConfig


def load_events_config(path: Path = DEFAULT_EVENTS_CONFIG_PATH) -> EventsConfig:
    """Load the reconstruction parameters from a TOML file."""
    with path.open("rb") as f:
        return EventsConfig.model_validate(tomllib.load(f))
