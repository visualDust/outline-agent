from __future__ import annotations

import sys

from loguru import logger

from .config import AppSettings

def configure_logging(settings: AppSettings) -> None:
    logger.remove()
    logger.add(
        sys.__stderr__,
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


__all__ = ["configure_logging", "logger"]
