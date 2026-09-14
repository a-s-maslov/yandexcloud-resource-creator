#!/usr/bin/env python3
"""Provision workshop identities and resources in Yandex Cloud."""

import argparse
import os
import sys
import time
import traceback

from auth import resolve_iam_token
from config import (
    Constants,
    env_bool,
    env_int,
    get_environment_config,
    load_environment,
)
from exceptions import ConfigurationError, UserCreationError, ValidationError
from logging_config import setup_logging
from modes import (
    run_create_folders_mode,
    run_delete_ydb_mode,
    run_generate_load_mode,
    run_reset_password_mode,
    run_serverless_ydb_mode,
    run_users_mode,
    run_ydb_mode,
)
from user_creator import UserCreator

logger = setup_logging()


def _workshop_prefix_from_env() -> str:
    """Prefer the resource-wide setting while accepting the old name safely."""
    prefix = os.getenv("YC_WORKSHOP_PREFIX", "").strip()
    legacy_prefix = os.getenv("YC_USER_PREFIX", "").strip()
    if prefix and legacy_prefix and prefix != legacy_prefix:
        raise ConfigurationError("YC_WORKSHOP_PREFIX and deprecated YC_USER_PREFIX must not differ")
    if legacy_prefix and not prefix:
        logger.warning("YC_USER_PREFIX is deprecated; rename it to YC_WORKSHOP_PREFIX")
    return prefix or legacy_prefix or Constants.DEFAULT_WORKSHOP_PREFIX


def _load_dotenv_from_argv() -> str:
    """Load .env early enough for its values to become argparse defaults."""
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--env-file", default=".env")
    pre_args, _ = pre_parser.parse_known_args()
    load_environment(pre_args.env_file)
    return pre_args.env_file


def create_argument_parser(env_file: str = ".env") -> argparse.ArgumentParser:
    """Create and configure argument parser."""
    parser = argparse.ArgumentParser(
        description="Yandex Cloud User and YDB Management Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Mode selection
    parser.add_argument(
        "--do",
        choices=[
            "users",
            "create-folders",
            "create-ydb",
            "create-ydb-serverless",
            "delete-ydb",
            "reset-password",
            "generate-load",
        ],
        default=os.getenv("YC_OPERATION"),
        help="Operation mode (env: YC_OPERATION)",
    )
    parser.add_argument("--env-file", default=env_file, help="Dotenv file to load (default: .env)")

    # Common arguments
    parser.add_argument(
        "--cloud-id", default=os.getenv("YC_CLOUD_ID"), help="Cloud ID (env: YC_CLOUD_ID)"
    )
    parser.add_argument(
        "--domain",
        default=os.getenv("YC_USER_DOMAIN"),
        help="Optional exact User Pool domain override (env: YC_USER_DOMAIN)",
    )
    parser.add_argument(
        "--created-users-file",
        default=os.getenv("YC_CREATED_USERS_FILE", Constants.DEFAULT_CREATED_USERS_FILE),
        help="Credentials CSV (env: YC_CREATED_USERS_FILE)",
    )
    parser.add_argument(
        "--resource-manifest-file",
        default=os.getenv(
            "YC_RESOURCE_MANIFEST_FILE",
            Constants.DEFAULT_RESOURCE_MANIFEST_FILE,
        ),
        help="Non-secret participant resource manifest (env: YC_RESOURCE_MANIFEST_FILE)",
    )
    parser.add_argument(
        "--overwrite-output",
        action="store_true",
        default=env_bool("YC_OVERWRITE_OUTPUT", False),
        help="Allow overwriting an output CSV (env: YC_OVERWRITE_OUTPUT)",
    )

    # User creation mode arguments
    parser.add_argument(
        "--userpool-id",
        default=os.getenv("YC_USERPOOL_ID"),
        help="Optional existing User Pool ID (env: YC_USERPOOL_ID)",
    )
    parser.add_argument(
        "--userpool-name",
        default=os.getenv("YC_USERPOOL_NAME", Constants.DEFAULT_USERPOOL_NAME),
        help="User Pool name used for find-or-create (env: YC_USERPOOL_NAME)",
    )
    parser.add_argument(
        "--userpool-subdomain",
        default=os.getenv(
            "YC_USERPOOL_SUBDOMAIN",
            Constants.DEFAULT_USERPOOL_SUBDOMAIN,
        ),
        help="Default subdomain used when creating the User Pool (env: YC_USERPOOL_SUBDOMAIN)",
    )
    parser.add_argument(
        "--create-userpool",
        action="store_true",
        default=env_bool("YC_CREATE_USERPOOL", False),
        help="Create the User Pool when it does not exist (env: YC_CREATE_USERPOOL)",
    )
    parser.add_argument(
        "--num-users",
        type=int,
        default=env_int("YC_NUM_USERS"),
        help="Number of users to create (env: YC_NUM_USERS)",
    )
    parser.add_argument(
        "--workshop-prefix",
        "--user-prefix",
        dest="workshop_prefix",
        default=_workshop_prefix_from_env(),
        help=("Common namespace for workshop users and resources (env: YC_WORKSHOP_PREFIX)"),
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=env_int("YC_START_INDEX", Constants.DEFAULT_START_INDEX),
        help="First numeric user index (env: YC_START_INDEX)",
    )
    parser.add_argument(
        "--password-length",
        type=int,
        default=env_int("YC_PASSWORD_LENGTH", Constants.DEFAULT_PASSWORD_LENGTH),
        help="Generated password length (env: YC_PASSWORD_LENGTH)",
    )
    parser.add_argument(
        "--require-password-change",
        action=argparse.BooleanOptionalAction,
        default=env_bool("YC_REQUIRE_PASSWORD_CHANGE", True),
        help=(
            "Require changing the generated password on first login "
            "(env: YC_REQUIRE_PASSWORD_CHANGE)"
        ),
    )
    parser.add_argument(
        "--folder-role",
        default=os.getenv("YC_FOLDER_ROLE", Constants.DEFAULT_FOLDER_ROLE),
        help="Role assigned in each personal folder (env: YC_FOLDER_ROLE)",
    )
    parser.add_argument(
        "--user-expires-at",
        default=os.getenv("YC_USER_EXPIRES_AT"),
        help="RFC3339 account expiration timestamp (env: YC_USER_EXPIRES_AT)",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=env_bool("YC_DRY_RUN", False),
        help="Run remote preflight but do not create anything (env: YC_DRY_RUN)",
    )

    # Reset-password mode arguments
    parser.add_argument(
        "--user-ids",
        default=os.getenv("YC_USER_IDS"),
        help="Comma-separated list of user IDs to reset password for (optional)",
    )

    # YDB mode specific arguments
    parser.add_argument(
        "--skip-folder-ids",
        default=os.getenv("YC_SKIP_FOLDER_IDS"),
        help="Comma-separated list of folder IDs to skip during operations",
    )
    parser.add_argument(
        "--create-ydb-in-folders",
        default=os.getenv("YC_CREATE_YDB_IN_FOLDERS"),
        help="Comma-separated list of folder IDs to create YDB in (if provided, only these folders are processed)",
    )
    parser.add_argument(
        "--serverless-storage-size-limit-gb",
        type=int,
        default=env_int(
            "YC_SERVERLESS_STORAGE_SIZE_LIMIT_GB",
            Constants.DEFAULT_SERVERLESS_STORAGE_SIZE_LIMIT_GB,
        ),
        help="Serverless storage limit in GiB (env: YC_SERVERLESS_STORAGE_SIZE_LIMIT_GB)",
    )
    parser.add_argument(
        "--serverless-enable-throttling",
        action=argparse.BooleanOptionalAction,
        default=env_bool("YC_SERVERLESS_ENABLE_THROTTLING", True),
        help="Enable the per-database Serverless RU/s limit (env: YC_SERVERLESS_ENABLE_THROTTLING)",
    )
    parser.add_argument(
        "--serverless-throttling-rcu-limit",
        type=int,
        default=env_int(
            "YC_SERVERLESS_THROTTLING_RCU_LIMIT",
            Constants.DEFAULT_SERVERLESS_THROTTLING_RCU_LIMIT,
        ),
        help="Serverless request-unit limit per second (env: YC_SERVERLESS_THROTTLING_RCU_LIMIT)",
    )
    parser.add_argument(
        "--serverless-provisioned-rcu-limit",
        type=int,
        default=env_int(
            "YC_SERVERLESS_PROVISIONED_RCU_LIMIT",
            Constants.DEFAULT_SERVERLESS_PROVISIONED_RCU_LIMIT,
        ),
        help="Provisioned Serverless RCU capacity; 0 disables hourly capacity billing (env: YC_SERVERLESS_PROVISIONED_RCU_LIMIT)",
    )

    # Generate-load and delete-ydb mode arguments
    parser.add_argument(
        "--folder-ids",
        default=os.getenv("YC_FOLDER_IDS"),
        help="Comma-separated list of folder IDs to target (required for delete-ydb mode, optional for generate-load mode)",
    )
    parser.add_argument(
        "--confirm-delete-ydb",
        action=argparse.BooleanOptionalAction,
        default=env_bool("YC_CONFIRM_DELETE_YDB", False),
        help="Required with --no-dry-run to delete YDB databases (env: YC_CONFIRM_DELETE_YDB)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=env_int("YC_BATCH_SIZE", Constants.DEFAULT_BATCH_SIZE),
        help=f"Parallel commands per batch (1-32) for generate-load mode (default: {Constants.DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("YC_OUTPUT_DIR", Constants.DEFAULT_OUTPUT_DIR),
        help=f"Existing writable directory to write bash scripts (generate-load mode) (default: {Constants.DEFAULT_OUTPUT_DIR})",
    )

    return parser


def main() -> None:
    """Main function with improved structure and error handling."""
    start_time = time.time()

    try:
        # Load .env, then parse CLI flags as optional overrides.
        env_file = _load_dotenv_from_argv()
        parser = create_argument_parser(env_file)
        args = parser.parse_args()
        if not args.do:
            parser.error("operation is required: set YC_OPERATION or pass --do")

        # Validate environment configuration
        try:
            env_config = get_environment_config()
        except ValueError as e:
            logger.error(f"Configuration error: {e}")
            sys.exit(1)

        # Initialize components
        iam_token = resolve_iam_token(env_config)
        user_creator = UserCreator(
            iam_token,
            request_timeout=env_config.request_timeout_seconds,
        )

        # Route to appropriate mode handler
        mode_handlers = {
            "users": run_users_mode,
            "create-folders": run_create_folders_mode,
            "create-ydb": run_ydb_mode,
            "create-ydb-serverless": run_serverless_ydb_mode,
            "delete-ydb": run_delete_ydb_mode,
            "reset-password": run_reset_password_mode,
            "generate-load": run_generate_load_mode,
        }

        handler = mode_handlers[args.do]
        handler(args, user_creator)

    except ValidationError as e:
        logger.error(f"Validation error: {e}")
        sys.exit(1)
    except ConfigurationError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except UserCreationError as e:
        logger.error(f"User creation error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Operation cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        logger.debug(f"Traceback: {traceback.format_exc()}")
        sys.exit(1)
    finally:
        elapsed = time.time() - start_time
        logger.info(f"Program completed in {elapsed:.2f}s")


if __name__ == "__main__":
    main()
