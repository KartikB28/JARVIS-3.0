"""
Centralized logging configuration for CHAPPIE.
"""

import logging
import sys
from pathlib import Path


_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    Get (or create) a logger that writes to both stdout and logs/chappie.log.

    Repeated calls with the same name return the same logger without
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

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "chappie.log")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
