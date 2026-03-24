"""
Logging setup for the pipeline.
"""
import logging
import os
import sys
from typing import Optional


def setup_logger(name: str = "pdftext", log_file: Optional[str] = None, level: int = logging.INFO) -> logging.Logger:
    """
    Configure and return a logger.

    Args:
        name:     Logger name.
        log_file: If provided, also write logs to this file path.
        level:    Logging level (default: INFO).
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger  # Already configured

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # File handler (optional)
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


def get_logger(name: str = "pdftext") -> logging.Logger:
    return logging.getLogger(name)
