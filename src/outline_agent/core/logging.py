from __future__ import annotations

import sys

from loguru import logger

from .config import AppSettings

_LOGGING_CONFIGURED = False


def configure_logging(settings: AppSettings) -> None:
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level.upper(),
        enqueue=False,
        backtrace=False,
        diagnose=False,
    )
    logger.add(
        settings.log_file_path,
        level=settings.log_level.upper(),
        rotation="10 MB",
        retention=5,
        enqueue=False,
        backtrace=False,
        diagnose=False,
        encoding="utf-8",
    )
    _LOGGING_CONFIGURED = True


__all__ = ["configure_logging", "logger"]
