#!/usr/bin/env python3
"""
Common utility functions to reduce code duplication.
"""

import logging
import os
import time
from contextlib import contextmanager
from typing import Iterator, List, Set, TextIO

logger = logging.getLogger(__name__)


def parse_comma_separated_ids(ids_string: str) -> List[str]:
    """Parse comma-separated IDs into a clean list."""
    if not ids_string:
        return []
    return [fid.strip() for fid in ids_string.split(",") if fid.strip()]


def parse_skip_folder_ids(skip_string: str) -> Set[str]:
    """Parse skip folder IDs into a set."""
    return set(parse_comma_separated_ids(skip_string))


@contextmanager
def safe_file_writer(file_path: str) -> Iterator[TextIO]:
    """Context manager for safe file writing with proper cleanup."""
    f = None
    try:
        f = open(file_path, "w", encoding="utf-8", newline="")
        yield f
    except Exception as e:
        logger.error(f"Failed to write to file {file_path}: {e}")
        raise
    finally:
        if f is not None:
            try:
                f.close()
            except Exception:
                pass


def validate_output_directory(path: str) -> None:
    """Validate that output directory exists and is writable."""
    if not path:
        raise ValueError("Output directory cannot be empty")
    if not os.path.isdir(path):
        raise ValueError(f"Output directory {path} is not a directory")
    if not os.access(path, os.W_OK):
        raise ValueError(f"Output directory {path} is not writable")


def format_elapsed_time(start_time: float) -> str:
    """Format elapsed time in a consistent way."""
    elapsed = time.time() - start_time
    return f"{elapsed:.1f}s"


def validate_batch_size(batch_size: int) -> None:
    """Validate batch size parameter."""
    if batch_size < 1 or batch_size > 32:
        raise ValueError("Batch size must be between 1 and 32")


def create_folder_objects_from_ids(folder_ids: List[str]) -> List[dict]:
    """Create minimal folder objects from folder IDs."""
    return [{"id": fid, "name": fid} for fid in folder_ids]


def has_ydb_storage_groups(database: dict) -> bool:
    """Check if YDB database has storage groups configured."""
    dedicated = database.get("dedicatedDatabase") or {}
    storage = database.get("storageConfig") or dedicated.get("storageConfig") or {}
    opts = storage.get("storageOptions", [])
    for opt in opts:
        try:
            group_count = int(opt.get("groupCount", "0"))
            if group_count > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def has_dedicated_ydb_storage(database: dict) -> bool:
    """Return whether the resource is a dedicated YDB database.

    Dedicated databases created by this tool use one storage group, so checking
    for more than one group made the create operation non-idempotent.
    """
    return "dedicatedDatabase" in database or has_ydb_storage_groups(database)


def is_serverless_ydb(database: dict) -> bool:
    """Return whether the resource is a Serverless YDB database."""
    return "serverlessDatabase" in database


class OperationTimer:
    """Context manager for timing operations."""

    def __init__(self, operation_name: str):
        self.operation_name = operation_name
        self.start_time = None

    def __enter__(self):
        self.start_time = time.time()
        logger.info(f"Starting {self.operation_name}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time:
            elapsed = time.time() - self.start_time
            if exc_type is None:
                logger.info(f"Completed {self.operation_name} in {elapsed:.2f}s")
            else:
                logger.error(f"Failed {self.operation_name} after {elapsed:.2f}s: {exc_val}")


def log_operation_progress(current: int, total: int, operation_name: str) -> None:
    """Log progress for long-running operations."""
    percentage = (current / total) * 100 if total > 0 else 0
    logger.info(f"{operation_name}: {current}/{total} ({percentage:.1f}%)")
