"""Nerkhban application."""

from __future__ import annotations

import logging
import os
import time


def _configure_application_logging() -> None:
    """Send app diagnostics to stderr independently of Uvicorn access logs."""
    app_logger = logging.getLogger("app")
    level_name = os.getenv("APP_LOG_LEVEL", "INFO").upper()
    app_logger.setLevel(getattr(logging, level_name, logging.INFO))
    if any(getattr(handler, "_nerkhban_handler", False) for handler in app_logger.handlers):
        return
    handler = logging.StreamHandler()
    handler._nerkhban_handler = True  # type: ignore[attr-defined]
    formatter = logging.Formatter(
        "%(asctime)sZ level=%(levelname)s logger=%(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    app_logger.addHandler(handler)
    # Avoid duplicate records if a process manager also configures the root logger.
    app_logger.propagate = False


_configure_application_logging()
