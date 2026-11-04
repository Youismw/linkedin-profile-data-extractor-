"""Configuration management and validation for LinkedIn Profile Extractor.

Module Architecture & Inter-Module Communication:
==================================================
This module provides strongly-typed configuration loading and validation using
Pydantic v2 and Python's built-in `tomllib`. It merges layered configuration sources
(CLI flags > config.toml > sensible defaults) and enforces boundary validation.

Module Communications:
----------------------
1. Upstream Callers:
   - `profile_extractor.cli`: Calls `read_config_file()` and `load_config(overrides=...)`.
   - `profile_extractor.orchestrator`: Receives validated `ExtractorConfig` to direct run parameters.
   - Unit tests in `tests/test_contracts.py`.

2. Downstream Dependencies:
   - Python Standard Library (`tomllib`, `pathlib`).
   - Pydantic v2 (`BaseModel`, `Field`, `field_validator`, `model_validator`).
"""

import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExtractorConfig(BaseModel):
    """Runtime configuration model holding all pipeline parameters.

    Attributes:
        input_file: Path to candidate spreadsheet (.xlsx).
        output_dir: Directory where JSON profiles and Excel reports are saved.
        auth_state_path: Stored Playwright browser cookies (.auth/state.json).
        important_fields: Core fields evaluated for missingness in Meta block.
        delay_seconds_min: Minimum jitter delay between consecutive profile visits.
        delay_seconds_max: Maximum jitter delay between consecutive profile visits.
        page_timeout_seconds: Hard deadline per page before marking TIMEOUT.
        max_profiles: Cap on valid profiles to process (None = unlimited).
        headless: Whether Chromium runs invisibly or with visible window.
        stop_on_block: Halts pipeline immediately upon bot challenge/CAPTCHA.
        log_level: Severity threshold for logging (e.g., 'INFO', 'DEBUG').
    """

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
    delay_seconds_min: float = Field(default=1.0, ge=0.0)
    delay_seconds_max: float = Field(default=2.0, ge=0.0)
    page_timeout_seconds: float = Field(default=30.0, gt=0.0)
    max_profiles: int | None = Field(default=None)
    headless: bool = Field(default=True)
    deep_details: bool = Field(default=True)
    disextra: bool = Field(default=False)
    scroll_steps: int = Field(default=25, ge=0, le=100)
    prune_stale: bool = Field(default=False)
    stop_on_block: bool = Field(default=True)
    log_level: str = Field(default="INFO")

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level string against standard Python logging names."""
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
        """Normalize max_profiles: 0 or negative values mean unlimited (None)."""
        if v is not None and v <= 0:
            return None
        return v

    @field_validator("important_fields")
    @classmethod
    def validate_important_fields(cls, v: Sequence[str]) -> list[str]:
        """Ensure important fields list is non-empty and whitespace-stripped."""
        cleaned = [field.strip() for field in v if field and field.strip()]
        if not cleaned:
            raise ValueError("important_fields list cannot be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_delay_bounds(self) -> "ExtractorConfig":
        """Enforce delay bounds: minimum delay must not exceed maximum delay."""
        if self.delay_seconds_min > self.delay_seconds_max:
            raise ValueError(
                f"delay_seconds_min ({self.delay_seconds_min}) cannot be greater than "
                f"delay_seconds_max ({self.delay_seconds_max})"
            )
        return self


def read_config_file(config_path: Path | str | None = None) -> dict[str, Any]:
    """Return raw key/values from the TOML config file (explicit path or ./config.toml).

    Returns an empty dict when no default config.toml exists. Raises FileNotFoundError
    if an explicitly requested config file is missing.
    """
    if config_path:
        path = Path(config_path)
        if not path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {path}")
        with path.open("rb") as f:
            return tomllib.load(f)
    default_file = Path("config.toml")
    if default_file.is_file():
        with default_file.open("rb") as f:
            return tomllib.load(f)
    return {}


def load_config(
    config_path: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> ExtractorConfig:
    """Load configuration from TOML with fallbacks and CLI overrides.

    Order of evaluation:
    1. Built-in defaults in ExtractorConfig model.
    2. config.toml file on disk (if present or explicitly passed).
    3. Programmatic CLI overrides dictionary (takes top precedence).
    """
    # 1. Load from explicit path or default 'config.toml'
    data: dict[str, Any] = dict(read_config_file(config_path))

    # 2. Apply runtime CLI overrides
    if overrides:
        for k, v in overrides.items():
            if v is not None:
                data[k] = v

    # 3. Validate against Pydantic schema
    return ExtractorConfig.model_validate(data)
