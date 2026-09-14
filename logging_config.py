#!/usr/bin/env python3
"""
Centralized logging configuration.
"""

import logging
import sys
from typing import Optional


def setup_logging(level: int = logging.INFO, format_string: Optional[str] = None) -> logging.Logger:
    """Setup consistent logging configuration."""
    if format_string is None:
        format_string = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    logging.basicConfig(
        level=level, format=format_string, stream=sys.stdout, datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Reduce noise from third-party libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    return logging.getLogger(__name__)


def get_logger(name: str) -> logging.Logger:
    """Get a logger with consistent configuration."""
    return logging.getLogger(name)
