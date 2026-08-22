"""Rotating local logfile. Writes only under the app data directory."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "claude_usage"


def setup_logging(log_file: Path, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        str(log_file), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    )
    logger.addHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    logger.addHandler(console)

    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)
