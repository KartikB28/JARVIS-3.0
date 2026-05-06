"""
Centralized logging configuration for CHAPPIE.
"""

import logging
import os
import sys
from pathlib import Path


_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _resolve_log_dir() -> Path:
    """Honor CHAPPIE_LOG_DIR (set by the desktop launcher) so the
    packaged .exe writes logs to the per-user folder, not next to itself."""
    override = os.environ.get("CHAPPIE_LOG_DIR")
    if override:
        return Path(override)
    return Path("logs")


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Get (or create) a logger that writes to both stdout and a chappie.log
    file. Repeated calls with the same name return the same logger without
    re-adding handlers.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    try:
        log_dir = _resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "chappie.log")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # If we can't write a log file (read-only filesystem etc), keep
        # console-only logging — don't crash the app.
        pass

    return logger
