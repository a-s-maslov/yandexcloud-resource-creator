#!/usr/bin/env python3
"""
Validation functions for the Yandex Cloud User Creation CLI Tool
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from config import Constants
from exceptions import ValidationError

logger = logging.getLogger(__name__)


def validate_userpool_id(userpool_id: str) -> None:
    """Validate user pool ID"""
    if not userpool_id:
        raise ValidationError("User pool ID cannot be empty")

    if len(userpool_id) > Constants.MAX_USERPOOL_ID_LENGTH:
        raise ValidationError(
            f"User pool ID cannot be longer than {Constants.MAX_USERPOOL_ID_LENGTH} characters"
        )

    if not re.match(r"^[a-zA-Z0-9]+$", userpool_id):
        raise ValidationError("User pool ID must contain only letters and digits")


def validate_number_of_users(num_users: int) -> None:
    """Validate number of users"""
    if num_users <= 0:
        raise ValidationError("Number of users must be greater than zero")

    if num_users > Constants.MAX_USERS_PER_BATCH:
        raise ValidationError(
            f"Number of users cannot be greater than {Constants.MAX_USERS_PER_BATCH}"
        )


def validate_domain(domain: str) -> None:
    """Validate domain name syntax"""
    if not domain:
        raise ValidationError("Domain name cannot be empty")

    # Basic domain validation regex
    domain_pattern = r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"

    if not re.match(domain_pattern, domain):
        raise ValidationError("Invalid domain name syntax")


def validate_created_users_file(created_users_file: str, overwrite: bool = False) -> None:
    """Validate created users file and check if it can be written to"""
    if not created_users_file:
        raise ValidationError("Created users file path cannot be empty")

    # Check that the directory is writable
    file_dir = os.path.dirname(created_users_file)
    if not file_dir:
        file_dir = "."

    if not os.access(file_dir, os.W_OK):
        raise ValidationError(f"Directory {file_dir} is not writable")

    # Check if file exists and is writable
    if os.path.exists(created_users_file):
        if not os.access(created_users_file, os.W_OK):
            raise ValidationError(f"Created users file {created_users_file} is not writable")

        if not overwrite:
            raise ValidationError(
                f"Output file {created_users_file} already exists. "
                "Set YC_OVERWRITE_OUTPUT=true or choose another file."
            )


def validate_workshop_prefix(prefix: str) -> None:
    """Validate the common namespace used for workshop resources."""
    if not prefix:
        raise ValidationError("Workshop prefix cannot be empty")
    if len(prefix) > 50:
        raise ValidationError("Workshop prefix cannot be longer than 50 characters")
    if not re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]|[a-z]", prefix):
        raise ValidationError(
            "Workshop prefix must start with a lowercase letter and contain only "
            "lowercase letters, digits, and hyphens"
        )


# Compatibility for callers that imported the old public helper.
validate_user_prefix = validate_workshop_prefix


def validate_password_length(length: int) -> None:
    """Validate password length against this workshop pool's policy."""
    if length < Constants.MIN_PASSWORD_LENGTH or length > Constants.MAX_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password length must be between {Constants.MIN_PASSWORD_LENGTH} "
            f"and {Constants.MAX_PASSWORD_LENGTH} characters"
        )


def validate_role_id(role_id: str) -> None:
    """Validate a Yandex Cloud role identifier supplied through configuration."""
    if not role_id or not re.fullmatch(r"[a-zA-Z0-9.-]+", role_id):
        raise ValidationError("Role ID contains unsupported characters")


def validate_organization_id(organization_id: str) -> None:
    """Validate an organization identifier returned by Resource Manager."""
    if not organization_id:
        raise ValidationError("Organization ID cannot be empty")
    if len(organization_id) > Constants.MAX_ORGANIZATION_ID_LENGTH:
        raise ValidationError(
            "Organization ID cannot be longer than "
            f"{Constants.MAX_ORGANIZATION_ID_LENGTH} characters"
        )
    if not re.fullmatch(r"[a-zA-Z0-9]+", organization_id):
        raise ValidationError("Organization ID must contain only letters and digits")


def validate_userpool_name(name: str) -> None:
    """Validate the API name used to find or create a User Pool."""
    if not name or not re.fullmatch(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?", name):
        raise ValidationError(
            "User Pool name must be 1-63 characters, start with a lowercase "
            "letter, and contain only lowercase letters, digits, and hyphens"
        )


def validate_userpool_subdomain(subdomain: str) -> None:
    """Validate the default Yandex-managed User Pool subdomain."""
    if not subdomain or len(subdomain) > 63:
        raise ValidationError("User Pool subdomain must be 1-63 characters")
    if not re.fullmatch(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?", subdomain):
        raise ValidationError(
            "User Pool subdomain must start with a lowercase letter and contain "
            "only lowercase letters, digits, and hyphens"
        )


def validate_user_expiration(expires_at: Optional[str]) -> None:
    """Validate a future RFC3339 timestamp for automatic account blocking."""
    if not expires_at:
        return
    try:
        value = expires_at.strip()
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("User expiration must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError("User expiration must include a timezone")
    if parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise ValidationError("User expiration must be in the future")


def validate_cloud_id(cloud_id: str) -> None:
    """Validate cloud ID"""
    if not cloud_id:
        raise ValidationError("Cloud ID cannot be empty")

    if len(cloud_id) > Constants.MAX_CLOUD_ID_LENGTH:
        raise ValidationError(
            f"Cloud ID cannot be longer than {Constants.MAX_CLOUD_ID_LENGTH} characters"
        )

    if not re.match(r"^[a-zA-Z0-9]+$", cloud_id):
        raise ValidationError("Cloud ID must contain only letters and digits")


def validate_batch_size(batch_size: int) -> None:
    """Validate batch size parameter"""
    if batch_size < 1 or batch_size > 32:
        raise ValidationError("Batch size must be between 1 and 32")


def validate_output_directory(output_dir: str) -> None:
    """Validate output directory exists and is writable"""
    if not output_dir:
        raise ValidationError("Output directory cannot be empty")

    if not os.path.isdir(output_dir):
        raise ValidationError(f"Output directory {output_dir} is not a directory")

    if not os.access(output_dir, os.W_OK):
        raise ValidationError(f"Output directory {output_dir} is not writable")
