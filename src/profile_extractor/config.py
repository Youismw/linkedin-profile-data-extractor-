"""Configuration management and validation for LinkedIn Profile Extractor."""

import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExtractorConfig(BaseModel):
    """Runtime configuration settings."""

    model_config = ConfigDict(extra="ignore")

    input_file: Path = Field(default=Path("input/profiles.xlsx"))
    output_dir: Path = Field(default=Path("output"))
    auth_state_path: Path = Field(default=Path(".auth/state.json"))
    important_fields: list[str] = Field(
        default_factory=lambda: [
            "name",
            "headline",
            "location",
            "about",
            "experience",
            "education",
            "skills",
        ]
    )
    delay_seconds_min: float = Field(default=5.0, ge=0.0)
    delay_seconds_max: float = Field(default=12.0, ge=0.0)
    page_timeout_seconds: float = Field(default=30.0, gt=0.0)
    max_profiles: int | None = Field(default=None)
    headless: bool = Field(default=True)
    stop_on_block: bool = Field(default=True)
    log_level: str = Field(default="INFO")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level against standard names."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper_v = v.upper()
        if upper_v not in valid_levels:
            raise ValueError(
                f"Invalid log_level '{v}'. Must be one of: {', '.join(sorted(valid_levels))}"
            )
        return upper_v

    @field_validator("max_profiles")
    @classmethod
    def validate_max_profiles(cls, v: int | None) -> int | None:
        """Ensure max_profiles is positive if specified (0 or None means unlimited)."""
        if v is not None and v <= 0:
            return None
        return v

    @field_validator("important_fields")
    @classmethod
    def validate_important_fields(cls, v: Sequence[str]) -> list[str]:
        """Ensure important fields list is not empty and stripped of spaces."""
        cleaned = [field.strip() for field in v if field and field.strip()]
        if not cleaned:
            raise ValueError("important_fields list cannot be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_delay_bounds(self) -> "ExtractorConfig":
        """Ensure minimum delay does not exceed maximum delay."""
        if self.delay_seconds_min > self.delay_seconds_max:
            raise ValueError(
                f"delay_seconds_min ({self.delay_seconds_min}) cannot be greater than "
                f"delay_seconds_max ({self.delay_seconds_max})"
            )
        return self


def load_config(
    config_path: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> ExtractorConfig:
    """Load configuration from a TOML file with defaults, applying optional overrides."""
    data: dict[str, Any] = {}

    if config_path:
        path = Path(config_path)
        if not path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {path}")
        with path.open("rb") as f:
            data = tomllib.load(f)
    else:
        # Check default config.toml if it exists in current working directory
        default_file = Path("config.toml")
        if default_file.is_file():
            with default_file.open("rb") as f:
                data = tomllib.load(f)

    if overrides:
        for k, v in overrides.items():
            if v is not None:
                data[k] = v

    return ExtractorConfig.model_validate(data)
