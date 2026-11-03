"""Logging configuration ensuring privacy and structured execution logs."""

import logging
import sys
from pathlib import Path

LOGGER_NAME = "profile_extractor"


def setup_logging(
    level: str = "INFO",
    log_dir: Path | str | None = "logs",
) -> logging.Logger:
    """Configure structured logging for the application.

    Ensures that at INFO level only high-level row progress and outcomes are logged.
    Full URLs appear only at DEBUG level. Profile content is never logged.
    """
    logger = logging.getLogger(LOGGER_NAME)
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    # Avoid adding duplicate handlers on multiple setup calls
    if logger.handlers:
        logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File Handler (if log_dir is provided)
    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path / "extractor.log", encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger() -> logging.Logger:
    """Retrieve the application logger."""
    return logging.getLogger(LOGGER_NAME)
