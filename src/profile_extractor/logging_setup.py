"""Logging configuration ensuring privacy and structured execution logs.

Module Architecture & Inter-Module Communication:
==================================================
This module establishes centralized, privacy-safe logging across all application components.
It enforces privacy rules by ensuring sensitive candidate details are never leaked to logs,
and configures unified log formatting for both terminal display and persistent audit files.

Module Communications:
----------------------
1. Upstream Callers:
   - `profile_extractor.cli`: Calls `setup_logging(level=config.log_level)` during startup.
   - All modules (`orchestrator.py`, `playwright_fetcher.py`, `reader.py`, `writer.py`):
     Call `get_logger()` to obtain the configured singleton logger instance.

2. Downstream Dependencies:
   - Python Standard Library (`logging`, `sys`, `pathlib`).
"""

import logging
import sys
from pathlib import Path

LOGGER_NAME = "profile_extractor"


def setup_logging(
    level: str = "INFO",
    log_dir: Path | str | None = "logs",
) -> logging.Logger:
    """Configure structured logging handlers and formatters.

    Parameters:
        level: Log severity threshold string ('DEBUG', 'INFO', 'WARNING', 'ERROR').
        log_dir: Directory to save persistent log files, or None for console-only.

    Returns:
        The configured root application Logger.
    """
    logger = logging.getLogger(LOGGER_NAME)
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    # Clear any pre-existing handlers to prevent duplicate lines if reconfigured
    if logger.handlers:
        logger.handlers.clear()

    # Standard clean timestamped format
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Console Stream Handler (direct feedback in user's terminal)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 2. File Handler (persistent audit log in logs/extractor.log)
    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path / "extractor.log", encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger() -> logging.Logger:
    """Retrieve the shared application logger instance."""
    return logging.getLogger(LOGGER_NAME)
