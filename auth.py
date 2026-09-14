#!/usr/bin/env python3
"""Authentication helpers for user and service-account credentials."""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import jwt
import requests

from config import EnvironmentConfig
from exceptions import ConfigurationError

IAM_TOKEN_URL = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
IAM_TOKEN_AUDIENCE = IAM_TOKEN_URL
JWT_LIFETIME_SECONDS = 3600


def load_service_account_key(key_file: str) -> Dict[str, Any]:
    """Load and validate the non-secret structure of an authorized key file."""
    path = Path(key_file).expanduser()
    if not path.is_file():
        raise ConfigurationError(f"Service account key file not found: {path}")

    try:
        with path.open(encoding="utf-8") as handle:
            key_data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot read service account key file {path}: {exc}") from exc

    normalized = {
        "id": key_data.get("id"),
        "service_account_id": (
            key_data.get("service_account_id") or key_data.get("serviceAccountId")
        ),
        "private_key": key_data.get("private_key") or key_data.get("privateKey"),
        "key_algorithm": (key_data.get("key_algorithm") or key_data.get("keyAlgorithm")),
    }
    missing = [
        field for field in ("id", "service_account_id", "private_key") if not normalized[field]
    ]
    if missing:
        raise ConfigurationError(
            f"Service account key file {path} is missing: {', '.join(missing)}"
        )

    algorithm = normalized["key_algorithm"]
    if algorithm and algorithm not in {"RSA_2048", "RSA_4096"}:
        raise ConfigurationError(f"Unsupported authorized key algorithm in {path}: {algorithm}")
    return normalized


def create_service_account_iam_token(
    key_file: str,
    request_timeout: float,
    session: Optional[requests.Session] = None,
    now: Optional[int] = None,
) -> str:
    """Exchange a service-account signed JWT for a Yandex Cloud IAM token."""
    key_data = load_service_account_key(key_file)
    issued_at = int(time.time()) if now is None else now
    payload = {
        "aud": IAM_TOKEN_AUDIENCE,
        "iss": key_data["service_account_id"],
        "iat": issued_at,
        "exp": issued_at + JWT_LIFETIME_SECONDS,
    }
    headers = {"kid": key_data["id"]}

    try:
        encoded_jwt = jwt.encode(
            payload,
            key_data["private_key"],
            algorithm="PS256",
            headers=headers,
        )
    except Exception as exc:
        raise ConfigurationError(
            f"Failed to sign JWT with service account key {key_file}: {exc}"
        ) from exc

    http = session or requests.Session()
    try:
        response = http.post(
            IAM_TOKEN_URL,
            json={"jwt": encoded_jwt},
            timeout=request_timeout,
        )
        response.raise_for_status()
        response_data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ConfigurationError(
            f"Failed to obtain IAM token for the service account: {exc}"
        ) from exc

    iam_token = response_data.get("iamToken")
    if not iam_token:
        raise ConfigurationError("IAM token response does not contain the iamToken field")
    return iam_token


def resolve_iam_token(config: EnvironmentConfig) -> str:
    """Resolve credentials, preferring an authorized service-account key."""
    if config.service_account_key_file:
        return create_service_account_iam_token(
            config.service_account_key_file,
            config.request_timeout_seconds,
        )
    if config.iam_token:
        return config.iam_token
    raise ConfigurationError("Set YC_SERVICE_ACCOUNT_KEY_FILE or IAM_TOKEN for authentication")
