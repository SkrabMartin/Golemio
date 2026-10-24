"""Thresholds of the audit, loaded from ``config/audit.toml``."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from headway.audit.findings import CATEGORIES

DEFAULT_AUDIT_CONFIG_PATH = Path("config/audit.toml")
SEVERITIES = ("error", "warning", "info")


class CollectionThresholds(BaseModel):
    """Thresholds for findings that come from the collector manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gap_factor: float = Field(gt=1, description="Gap = no successful poll for factor x interval.")
    gap_tolerance_s: float = Field(ge=0, description="Request latency allowed on top of that.")
    stale_feed_min_polls: int = Field(ge=2, description="Identical bodies in a row.")
    api_error_min_ticks: int = Field(ge=1, description="Failed ticks in a row.")


class PositionThresholds(BaseModel):
    """Thresholds for findings about individual positions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stale_age_s: float = Field(gt=0)
    off_shape_m: float = Field(gt=0)
    jump_min_dt_s: float = Field(gt=0)
    jump_spike_m: float = Field(gt=0)
    jump_spike_span_s: float = Field(gt=0)
    jump_speed_default_mps: float = Field(gt=0)
    jump_speed_mps: dict[str, float]

    def jump_speed(self, route_type: int) -> float:
        """Return the jump speed threshold for a GTFS route type."""
        return self.jump_speed_mps.get(str(route_type), self.jump_speed_default_mps)


class TripThresholds(BaseModel):
    """Thresholds for findings about whole trips and vehicles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    silent_min: float = Field(gt=0, description="Minutes without a new position.")
    overlap_s: float = Field(ge=0, description="Overlap of two trips of one vehicle.")
    early_end_m: float = Field(ge=0, description="Route left when a trip stops reporting.")
    explain_slack_s: float = Field(
        ge=0, description="Slack when matching an outage to a lost tail."
    )
    explain_speed_mps: float = Field(gt=0, description="Generous average speed along a route.")


class AuditConfig(BaseModel):
    """All audit thresholds and the severity of each category."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collection: CollectionThresholds
    positions: PositionThresholds
    trips: TripThresholds
    severity: dict[str, str]

    @field_validator("severity")
    @classmethod
    def _known_severities(cls, value: dict[str, str]) -> dict[str, str]:
        bad = {k: v for k, v in value.items() if v not in SEVERITIES}
        if bad:
            raise ValueError(f"severity must be one of {SEVERITIES}: {bad}")
        if set(value) != set(CATEGORIES):
            raise ValueError(f"severity needs exactly the categories {sorted(CATEGORIES)}")
        return value


def load_audit_config(path: Path = DEFAULT_AUDIT_CONFIG_PATH) -> AuditConfig:
    """Load the audit thresholds from a TOML file."""
    with path.open("rb") as f:
        return AuditConfig.model_validate(tomllib.load(f))
