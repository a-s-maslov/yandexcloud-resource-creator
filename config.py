#!/usr/bin/env python3
"""
Centralized configuration and validation for Yandex Cloud CLI tool.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


# Constants - make them clear and documented
class Constants:
    """Application constants."""

    # Load testing duration (10 hours in seconds)
    LOAD_DURATION_SECONDS = 36000

    # User limits and validation
    MAX_USERS_PER_BATCH = 100
    MAX_USERPOOL_ID_LENGTH = 50
    MAX_ORGANIZATION_ID_LENGTH = 50
    MAX_CLOUD_ID_LENGTH = 64

    # Operation limits
    MAX_CONCURRENT_OPERATIONS = 5
    MAX_POLL_RETRIES = 5

    # Default values
    DEFAULT_CREATED_USERS_FILE = "created_users.csv"
    DEFAULT_RESOURCE_MANIFEST_FILE = "workshop_resources.csv"
    DEFAULT_OUTPUT_DIR = "load"
    DEFAULT_BATCH_SIZE = 16
    DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
    DEFAULT_WORKSHOP_PREFIX = "ydb"
    DEFAULT_START_INDEX = 1
    DEFAULT_PASSWORD_LENGTH = 11
    MIN_PASSWORD_LENGTH = 11
    MAX_PASSWORD_LENGTH = 72
    DEFAULT_USERPOOL_NAME = "scale-2026-workshop"
    DEFAULT_USERPOOL_SUBDOMAIN = "scale-2026-workshop"
    DEFAULT_USER_DOMAIN_SUFFIX = "idp.yandexcloud.net"
    DEFAULT_FOLDER_ROLE = "ydb.editor"

    # YDB configuration
    YDB_AVAILABILITY_ZONES = ["ru-central1-a", "ru-central1-b", "ru-central1-d"]
    YDB_RESOURCE_PRESET = "small-m8"
    YDB_STORAGE_TYPE = "ssd"
    YDB_GROUP_COUNT = "1"
    YDB_SCALE_SIZE = "1"
    DEFAULT_SERVERLESS_STORAGE_SIZE_LIMIT_GB = 5
    DEFAULT_SERVERLESS_THROTTLING_RCU_LIMIT = 10
    DEFAULT_SERVERLESS_PROVISIONED_RCU_LIMIT = 0

    # Network configuration
    VPC_CIDR_BLOCKS = ["192.168.1.0/24", "192.168.2.0/24", "192.168.3.0/24"]


@dataclass
class EnvironmentConfig:
    """Environment configuration validation."""

    iam_token: Optional[str]
    service_account_key_file: Optional[str]
    request_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "EnvironmentConfig":
        """Create configuration from environment variables."""
        iam_token = os.getenv("IAM_TOKEN", "").strip()
        key_file = os.getenv("YC_SERVICE_ACCOUNT_KEY_FILE", "").strip()
        if not iam_token and not key_file:
            raise ValueError("Set YC_SERVICE_ACCOUNT_KEY_FILE or IAM_TOKEN for authentication")
        raw_timeout = os.getenv(
            "YC_REQUEST_TIMEOUT_SECONDS",
            str(Constants.DEFAULT_REQUEST_TIMEOUT_SECONDS),
        )
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ValueError("YC_REQUEST_TIMEOUT_SECONDS must be a number") from exc
        if timeout <= 0:
            raise ValueError("YC_REQUEST_TIMEOUT_SECONDS must be greater than zero")
        return cls(
            iam_token=iam_token or None,
            service_account_key_file=key_file or None,
            request_timeout_seconds=timeout,
        )


def load_environment(env_file: str = ".env") -> None:
    """Load configuration from a dotenv file without overriding exported values."""
    load_dotenv(dotenv_path=env_file, override=False)


def env_bool(name: str, default: bool = False) -> bool:
    """Read a conventional boolean value from the environment."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: true, false, 1, 0, yes, no, on, off")


def env_int(name: str, default=None):
    """Read an optional integer from the environment."""
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def validate_required_env() -> EnvironmentConfig:
    """Validate all required environment variables."""
    return EnvironmentConfig.from_env()


def get_environment_config() -> EnvironmentConfig:
    """Get environment configuration with proper error handling."""
    try:
        return validate_required_env()
    except ValueError as e:
        logger.error(f"Environment configuration error: {e}")
        raise
