#!/usr/bin/env python3
"""
Mode handlers for the Yandex Cloud CLI tool.

Provides run_users_mode and run_ydb_mode entry points used by main.py.
"""

import csv
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional

from config import Constants
from exceptions import PermanentPasswordError, UserCreationError, ValidationError
from operation_poller import OperationPoller
from user_creator import UserCreator
from utils import (
    OperationTimer,
    create_folder_objects_from_ids,
    has_dedicated_ydb_storage,
    has_ydb_storage_groups,
    is_serverless_ydb,
    log_operation_progress,
    parse_comma_separated_ids,
    parse_skip_folder_ids,
    safe_file_writer,
)
from validators import (
    validate_batch_size,
    validate_cloud_id,
    validate_created_users_file,
    validate_domain,
    validate_number_of_users,
    validate_organization_id,
    validate_output_directory,
    validate_password_length,
    validate_role_id,
    validate_user_expiration,
    validate_userpool_id,
    validate_userpool_name,
    validate_userpool_subdomain,
    validate_workshop_prefix,
)

logger = logging.getLogger(__name__)


def _workshop_index_from_folder_name(folder_name: str, prefix: str) -> Optional[int]:
    """Return the participant index from a canonical workshop folder name."""
    match = re.fullmatch(rf"{re.escape(prefix)}-f-(\d+)", folder_name)
    return int(match.group(1)) if match else None


def _resolve_ydb_target_folders(args, user_creator: UserCreator) -> list:
    """Resolve selected folder IDs to metadata so generated names stay readable."""
    if getattr(args, "create_ydb_in_folders", None):
        folder_ids = parse_comma_separated_ids(args.create_ydb_in_folders)
        folders = [user_creator.get_folder(folder_id) for folder_id in folder_ids]
        foreign = [
            folder.get("id", "")
            for folder in folders
            if folder.get("cloudId") and folder.get("cloudId") != args.cloud_id
        ]
        if foreign:
            raise ValidationError("Selected folders belong to another cloud: " + ", ".join(foreign))
        invalid = [
            folder.get("name", folder.get("id", "unknown"))
            for folder in folders
            if _workshop_index_from_folder_name(folder.get("name", ""), args.workshop_prefix)
            is None
        ]
        if invalid:
            raise ValidationError(
                "Selected folders do not match the workshop naming scheme: " + ", ".join(invalid)
            )
        return folders

    folders = user_creator.list_folders(args.cloud_id)
    selected = [
        folder
        for folder in folders
        if _workshop_index_from_folder_name(folder.get("name", ""), args.workshop_prefix)
        is not None
    ]
    logger.info(
        "Selected %d workshop folder(s); ignored %d unrelated folder(s)",
        len(selected),
        len(folders) - len(selected),
    )
    return selected


def _ydb_database_name(folder_name: str, prefix: str) -> str:
    """Build a database name independently from a canonical folder name."""
    index = _workshop_index_from_folder_name(folder_name, prefix)
    if index is None:
        raise ValidationError(f"Folder {folder_name!r} does not match {prefix}-f-NNN")
    return f"{prefix}-db-{index:03d}"


def _vpc_network_name(folder_name: str, prefix: str) -> str:
    """Build a VPC name independently from a canonical folder name."""
    index = _workshop_index_from_folder_name(folder_name, prefix)
    if index is None:
        raise ValidationError(f"Folder {folder_name!r} does not match {prefix}-f-NNN")
    return f"{prefix}-vpc-{index:03d}"


@dataclass(frozen=True)
class WorkshopUserSpec:
    """Deterministic identity and folder names for one workshop participant."""

    index: int
    username: str
    given_name: str
    family_name: str
    full_name: str
    phone_number: str
    folder_name: str


@dataclass(frozen=True)
class UserPoolPlan:
    """Resolved existing pool, or an explicit plan to create one."""

    organization_id: str
    name: str
    subdomain: str
    domain: str
    userpool_id: Optional[str]
    existing: bool


def default_userpool_domain(subdomain: str) -> str:
    """Build the Yandex-managed domain created for a default subdomain."""
    return f"{subdomain}.{Constants.DEFAULT_USER_DOMAIN_SUFFIX}"


def _pool_plan_from_resource(
    pool: dict,
    organization_id: str,
    configured_domain: Optional[str],
    configured_subdomain: str,
) -> UserPoolPlan:
    """Validate an API User Pool resource and select its login domain."""
    pool_id = pool.get("id", "")
    validate_userpool_id(pool_id)
    if pool.get("organizationId") != organization_id:
        raise ValidationError(f"User Pool {pool_id} belongs to another organization")
    if pool.get("status") != "ACTIVE":
        raise ValidationError(
            f"User Pool {pool_id} is not ACTIVE (status: {pool.get('status', 'unknown')})"
        )

    domains = [domain for domain in pool.get("domains", []) if domain]
    if not domains:
        raise ValidationError(f"User Pool {pool_id} has no login domain")

    if configured_domain:
        validate_domain(configured_domain)
        matching_domain = next(
            (domain for domain in domains if domain.casefold() == configured_domain.casefold()),
            None,
        )
        if not matching_domain:
            raise ValidationError(
                f"Configured domain {configured_domain} is not attached to User Pool {pool_id}"
            )
        domain = matching_domain
    else:
        expected = default_userpool_domain(configured_subdomain)
        domain = next(
            (domain for domain in domains if domain.casefold() == expected.casefold()),
            domains[0],
        )
    validate_domain(domain)
    return UserPoolPlan(
        organization_id=organization_id,
        name=pool.get("name", ""),
        subdomain=configured_subdomain,
        domain=domain,
        userpool_id=pool_id,
        existing=True,
    )


def plan_workshop_userpool(args, user_creator: UserCreator) -> UserPoolPlan:
    """Resolve cloud -> organization and find the configured User Pool."""
    cloud = user_creator.get_cloud(args.cloud_id)
    cloud_organization_id = cloud.get("organizationId", "")
    validate_organization_id(cloud_organization_id)
    organization_id = cloud_organization_id

    if args.userpool_id:
        validate_userpool_id(args.userpool_id)
        pool = user_creator.get_userpool(args.userpool_id)
        return _pool_plan_from_resource(
            pool,
            organization_id,
            args.domain,
            args.userpool_subdomain,
        )

    validate_userpool_name(args.userpool_name)
    validate_userpool_subdomain(args.userpool_subdomain)
    pools = user_creator.list_userpools(organization_id)
    pool = next(
        (item for item in pools if item.get("name") == args.userpool_name),
        None,
    )
    if pool:
        return _pool_plan_from_resource(
            pool,
            organization_id,
            args.domain,
            args.userpool_subdomain,
        )

    if not args.create_userpool:
        raise ValidationError(
            f"User Pool {args.userpool_name!r} was not found. "
            "Set YC_CREATE_USERPOOL=true to allow its creation."
        )

    expected_domain = default_userpool_domain(args.userpool_subdomain)
    if args.domain and args.domain.casefold() != expected_domain.casefold():
        raise ValidationError(
            "YC_USER_DOMAIN can override a domain only for an existing User Pool; "
            f"a new pool with this subdomain will use {expected_domain}"
        )
    domain = args.domain or expected_domain
    validate_domain(domain)
    return UserPoolPlan(
        organization_id=organization_id,
        name=args.userpool_name,
        subdomain=args.userpool_subdomain,
        domain=domain,
        userpool_id=None,
        existing=False,
    )


def create_planned_userpool(
    plan: UserPoolPlan,
    configured_domain: Optional[str],
    user_creator: UserCreator,
) -> UserPoolPlan:
    """Materialize a previously preflighted User Pool plan."""
    pool = user_creator.create_userpool(
        organization_id=plan.organization_id,
        name=plan.name,
        default_subdomain=plan.subdomain,
    )
    return _pool_plan_from_resource(
        pool,
        plan.organization_id,
        configured_domain,
        plan.subdomain,
    )


def build_workshop_user_specs(
    num_users: int,
    start_index: int,
    prefix: str,
    domain: str,
) -> List[WorkshopUserSpec]:
    """Build a stable batch that can be reviewed before any API mutation."""
    if start_index <= 0:
        raise ValidationError("Start index must be greater than zero")

    specs = []
    for index in range(start_index, start_index + num_users):
        login = f"{prefix}_{index:03d}"
        folder_name = f"{prefix}-f-{index:03d}"
        if len(folder_name) > 63:
            raise ValidationError(f"Generated folder name {folder_name!r} is too long")
        given_name = "Workshop"
        family_name = f"User {index:03d}"
        specs.append(
            WorkshopUserSpec(
                index=index,
                username=f"{login}@{domain}",
                given_name=given_name,
                family_name=family_name,
                full_name=f"{given_name} {family_name}",
                phone_number="",
                folder_name=folder_name,
            )
        )
    return specs


def preflight_workshop_user_batch(
    specs: List[WorkshopUserSpec],
    userpool_id: Optional[str],
    cloud_id: str,
    user_creator: UserCreator,
) -> None:
    """Fail the whole batch before mutation if any target name already exists."""
    existing_users = user_creator.list_users_in_userpool(userpool_id) if userpool_id else []
    existing_folders = user_creator.list_folders(cloud_id)
    usernames = {user.get("username", "").casefold() for user in existing_users}
    folder_names = {folder.get("name", "").casefold() for folder in existing_folders}

    conflicts = []
    for spec in specs:
        if spec.username.casefold() in usernames:
            conflicts.append(f"user {spec.username}")
        if spec.folder_name.casefold() in folder_names:
            conflicts.append(f"folder {spec.folder_name}")

    if conflicts:
        sample = ", ".join(conflicts[:10])
        remainder = len(conflicts) - 10
        suffix = f" (and {remainder} more)" if remainder > 0 else ""
        raise ValidationError(
            f"Preflight found {len(conflicts)} existing target(s): {sample}{suffix}. "
            "Change YC_WORKSHOP_PREFIX or YC_START_INDEX; nothing was created."
        )


def run_users_mode(args, user_creator: UserCreator) -> None:
    """Create deterministic workshop users, personal folders, and access."""
    if not args.num_users:
        raise ValidationError("--num-users is required for users mode")
    if not args.cloud_id:
        raise ValidationError("--cloud-id is required for users mode")

    validate_number_of_users(args.num_users)
    validate_cloud_id(args.cloud_id)
    validate_workshop_prefix(args.workshop_prefix)
    validate_password_length(args.password_length)
    validate_role_id(args.folder_role)
    validate_user_expiration(args.user_expires_at)

    pool_plan = plan_workshop_userpool(args, user_creator)
    logger.info(
        "Cloud %s belongs to organization %s",
        args.cloud_id,
        pool_plan.organization_id,
    )
    if args.user_expires_at:
        logger.info("Workshop accounts expire at %s", args.user_expires_at)

    specs = build_workshop_user_specs(
        num_users=args.num_users,
        start_index=args.start_index,
        prefix=args.workshop_prefix,
        domain=pool_plan.domain,
    )

    if pool_plan.existing:
        logger.info(
            "Using existing User Pool %s (%s), domain %s",
            pool_plan.name,
            pool_plan.userpool_id,
            pool_plan.domain,
        )
    else:
        logger.info(
            "User Pool %s is absent and will be created with domain %s",
            pool_plan.name,
            pool_plan.domain,
        )
    logger.info("Preflight: checking all usernames and folders before mutation")
    preflight_workshop_user_batch(
        specs,
        pool_plan.userpool_id,
        args.cloud_id,
        user_creator,
    )

    if args.dry_run:
        logger.info(
            "Dry run successful. Planned range: %s .. %s; no resources were created.",
            specs[0].username,
            specs[-1].username,
        )
        return

    validate_created_users_file(
        args.created_users_file,
        overwrite=args.overwrite_output,
    )

    if not pool_plan.existing:
        pool_plan = create_planned_userpool(
            pool_plan,
            args.domain,
            user_creator,
        )
        specs = build_workshop_user_specs(
            num_users=args.num_users,
            start_index=args.start_index,
            prefix=args.workshop_prefix,
            domain=pool_plan.domain,
        )
        logger.info(
            "Created User Pool %s (%s), domain %s",
            pool_plan.name,
            pool_plan.userpool_id,
            pool_plan.domain,
        )

    userpool_id = pool_plan.userpool_id
    if not userpool_id:
        raise UserCreationError("User Pool creation returned no ID")

    created_users = 0
    ready_users = 0
    failed_users = 0

    with OperationTimer("user creation"):
        with safe_file_writer(args.created_users_file) as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "index",
                    "user_id",
                    "username",
                    "password",
                    "folder_id",
                    "status",
                    "error",
                ]
            )
            f.flush()

            for position, spec in enumerate(specs):
                user_id = ""
                password = ""
                folder_id = ""
                status = "failed"
                error_message = ""
                try:
                    log_operation_progress(position, len(specs), "User creation")
                    password, generation_proof = user_creator.generate_password(
                        args.password_length
                    )
                    user_id = user_creator.create_user(
                        userpool_id=userpool_id,
                        username=spec.username,
                        full_name=spec.full_name,
                        given_name=spec.given_name,
                        family_name=spec.family_name,
                        email=spec.username,
                        phone_number=spec.phone_number,
                        password=password,
                        generation_proof=generation_proof,
                        expires_at=args.user_expires_at,
                    )

                    if not args.require_password_change:
                        try:
                            user_creator.set_user_password_hash(
                                user_id=user_id,
                                password=password,
                                need_change=False,
                            )
                        except UserCreationError as password_error:
                            try:
                                user_creator.delete_user(user_id)
                                user_id = ""
                            except UserCreationError as rollback_error:
                                raise UserCreationError(
                                    "Permanent password setup failed and the newly "
                                    "created user could not be rolled back: "
                                    f"{password_error}; rollback: {rollback_error}"
                                ) from password_error
                            raise PermanentPasswordError(
                                "Permanent password setup failed; the newly created "
                                f"user was rolled back: {password_error}"
                            ) from password_error

                    created_users += 1

                    try:
                        folder_id = user_creator.create_folder(
                            cloud_id=args.cloud_id,
                            folder_name=spec.folder_name,
                            description=f"Personal folder for {spec.username}",
                        )
                        user_creator.grant_folder_access(
                            folder_id=folder_id,
                            user_id=user_id,
                            role_id=args.folder_role,
                        )
                        user_creator.grant_cloud_access(
                            cloud_id=args.cloud_id,
                            user_id=user_id,
                            role_id="resource-manager.clouds.member",
                        )
                        status = "ready"
                        ready_users += 1
                        logger.info(f"Folder and access created for user {spec.username}")
                    except UserCreationError as e:
                        status = "partial"
                        error_message = str(e)
                        failed_users += 1
                        logger.error(
                            f"User {spec.username} was created but folder/access setup failed: {e}"
                        )
                except PermanentPasswordError:
                    raise
                except UserCreationError as e:
                    error_message = str(e)
                    failed_users += 1
                    logger.error(f"Failed to create user {spec.username}: {e}")
                finally:
                    writer.writerow(
                        [
                            spec.index,
                            user_id,
                            spec.username,
                            password,
                            folder_id,
                            status,
                            error_message,
                        ]
                    )
                    f.flush()

    logger.info(
        "User provisioning completed. Created users: %d; fully ready: %d; "
        "failed/partial: %d. Output: %s",
        created_users,
        ready_users,
        failed_users,
        args.created_users_file,
    )
    if failed_users:
        raise UserCreationError(
            f"{failed_users} participant(s) were not fully provisioned; "
            f"inspect {args.created_users_file} before distributing credentials"
        )


def run_create_folders_mode(args, user_creator: UserCreator) -> None:
    """Create or reuse personal folders for existing workshop users."""
    if not args.num_users:
        raise ValidationError("--num-users is required for create-folders mode")
    if not args.cloud_id:
        raise ValidationError("--cloud-id is required for create-folders mode")

    validate_number_of_users(args.num_users)
    validate_cloud_id(args.cloud_id)
    validate_workshop_prefix(args.workshop_prefix)
    validate_role_id(args.folder_role)

    pool_plan = plan_workshop_userpool(args, user_creator)
    if not pool_plan.existing or not pool_plan.userpool_id:
        raise ValidationError(
            "create-folders requires an existing User Pool and never creates users"
        )

    specs = build_workshop_user_specs(
        args.num_users,
        args.start_index,
        args.workshop_prefix,
        pool_plan.domain,
    )
    existing_users = user_creator.list_users_in_userpool(pool_plan.userpool_id)
    users_by_name = {user.get("username", "").casefold(): user for user in existing_users}
    missing_users = [
        spec.username for spec in specs if spec.username.casefold() not in users_by_name
    ]
    if missing_users:
        sample = ", ".join(missing_users[:10])
        remainder = len(missing_users) - 10
        suffix = f" (and {remainder} more)" if remainder > 0 else ""
        raise ValidationError(
            f"Missing {len(missing_users)} existing workshop user(s): "
            f"{sample}{suffix}. Nothing was created."
        )

    existing_folders = {
        folder.get("name", "").casefold(): folder
        for folder in user_creator.list_folders(args.cloud_id)
    }
    create_count = sum(spec.folder_name.casefold() not in existing_folders for spec in specs)
    logger.info(
        "Preflight successful: %d existing user(s), %d folder(s) to create, %d folder(s) to reuse",
        len(specs),
        create_count,
        len(specs) - create_count,
    )
    if args.dry_run:
        for spec in specs:
            action = "reuse" if spec.folder_name.casefold() in existing_folders else "create"
            logger.info(
                "DRY RUN: would %s folder %s and grant access to %s",
                action,
                spec.folder_name,
                spec.username,
            )
        return

    validate_created_users_file(
        args.resource_manifest_file,
        overwrite=args.overwrite_output,
    )
    failures = 0
    with safe_file_writer(args.resource_manifest_file) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "index",
                "user_id",
                "username",
                "folder_name",
                "folder_id",
                "database_name",
                "database_id",
                "status",
                "error",
            ]
        )
        handle.flush()
        for position, spec in enumerate(specs):
            user = users_by_name[spec.username.casefold()]
            user_id = user["id"]
            folder = existing_folders.get(spec.folder_name.casefold())
            folder_id = folder.get("id", "") if folder else ""
            status = "failed"
            error_message = ""
            try:
                log_operation_progress(position, len(specs), "Folder provisioning")
                if not folder_id:
                    folder_id = user_creator.create_folder(
                        cloud_id=args.cloud_id,
                        folder_name=spec.folder_name,
                        description=f"Personal folder for {spec.username}",
                    )
                user_creator.grant_folder_access(
                    folder_id=folder_id,
                    user_id=user_id,
                    role_id=args.folder_role,
                )
                user_creator.grant_cloud_access(
                    cloud_id=args.cloud_id,
                    user_id=user_id,
                    role_id="resource-manager.clouds.member",
                )
                status = "ready"
            except UserCreationError as exc:
                failures += 1
                error_message = str(exc)
                logger.error("Failed to provision %s: %s", spec.username, exc)
                if "429 Client Error" in error_message:
                    raise UserCreationError(
                        "Folder provisioning stopped after a quota or rate-limit "
                        f"response: {error_message}"
                    ) from exc
            finally:
                writer.writerow(
                    [
                        spec.index,
                        user_id,
                        spec.username,
                        spec.folder_name,
                        folder_id,
                        _ydb_database_name(spec.folder_name, args.workshop_prefix),
                        "",
                        status,
                        error_message,
                    ]
                )
                handle.flush()

    if failures:
        raise UserCreationError(
            f"Folder provisioning failed for {failures} participant(s); "
            f"inspect {args.resource_manifest_file}"
        )


def run_ydb_mode(args, user_creator: UserCreator) -> None:
    """Create dedicated YDB databases, or only describe changes in dry-run."""
    validate_cloud_id(args.cloud_id)
    validate_workshop_prefix(args.workshop_prefix)

    logger.info(f"Starting YDB creation mode for cloud {args.cloud_id}")

    # Parse skip folder IDs
    skip_folder_ids = parse_skip_folder_ids(args.skip_folder_ids)
    if skip_folder_ids:
        logger.info(f"Will skip folders: {skip_folder_ids}")

    # Determine which folders to process
    target_folders = _resolve_ydb_target_folders(args, user_creator)
    if getattr(args, "create_ydb_in_folders", None):
        logger.info(
            "Will create YDB only in specified folders: %s",
            [folder["id"] for folder in target_folders],
        )

    created_databases = 0
    planned_databases = 0
    skipped_folders = 0
    failed_folders = 0
    pending_ops = []  # list of dicts: {folder_id, folder_name, operation_id}
    poller = OperationPoller(user_creator)

    with OperationTimer("YDB creation"):
        for folder in target_folders:
            folder_id = folder["id"]
            folder_name = folder["name"]

            if folder_id in skip_folder_ids:
                logger.info(f"Skipping folder {folder_name} (ID: {folder_id})")
                skipped_folders += 1
                continue

            try:
                # Skip if folder already has a dedicated YDB
                existing_dbs = user_creator.list_ydb_databases_in_folder(folder_id)
                has_existing_dedicated = any(has_dedicated_ydb_storage(db) for db in existing_dbs)
                database_name = _ydb_database_name(folder_name, args.workshop_prefix)

                if has_existing_dedicated:
                    logger.info(
                        f"Folder {folder_name} (ID: {folder_id}) already has a dedicated YDB database. Skipping."
                    )
                    skipped_folders += 1
                    continue

                if any(db.get("name") == database_name for db in existing_dbs):
                    logger.warning(
                        f"Folder {folder_name} already has a database named "
                        f"{database_name}. Skipping to avoid a name conflict."
                    )
                    skipped_folders += 1
                    continue

                # Check for existing VPC or create new one
                network_id, subnet_ids = user_creator.check_existing_vpc(folder_id)

                if network_id and subnet_ids:
                    logger.info(
                        f"Using existing VPC {network_id} for folder {folder_name} (ID: {folder_id})"
                    )
                else:
                    if args.dry_run:
                        logger.info(
                            f"DRY RUN: would create a VPC and required subnets in "
                            f"folder {folder_name} (ID: {folder_id})"
                        )
                    else:
                        logger.info(f"Creating new VPC for folder {folder_name} (ID: {folder_id})")
                        network_id, subnet_ids = user_creator.create_vpc_with_subnets(
                            folder_id=folder_id,
                            network_name=_vpc_network_name(folder_name, args.workshop_prefix),
                            description=f"VPC network for folder {folder_name}",
                        )

                if args.dry_run:
                    logger.info(
                        f"DRY RUN: would create dedicated YDB database "
                        f"{database_name} in folder {folder_name}"
                    )
                    planned_databases += 1
                    continue

                if not network_id or not subnet_ids:
                    raise UserCreationError(
                        f"VPC preparation produced no network/subnets for folder {folder_id}"
                    )

                # Flow control: if we already have max concurrent ops in-flight, poll until one finishes
                while len(pending_ops) >= Constants.MAX_CONCURRENT_OPERATIONS:
                    poll_result = poller.poll_pending_operations(pending_ops, "create")
                    created_databases += poll_result.successes
                    failed_folders += poll_result.failures

                # Start YDB create operation (non-blocking)
                op_id = user_creator.start_ydb_database(
                    folder_id=folder_id,
                    network_id=network_id,
                    subnet_ids=subnet_ids,
                    database_name=database_name,
                    description=f"YDB database for folder {folder_name}",
                )

                pending_ops.append(
                    {
                        "folder_id": folder_id,
                        "folder_name": folder_name,
                        "operation_id": op_id,
                        "start_time": time.time(),
                    }
                )

            except UserCreationError as e:
                logger.error(f"Failed to start YDB for folder {folder_name} (ID: {folder_id}): {e}")
                failed_folders += 1
                continue

        # Finalize any remaining operations
        while pending_ops:
            poll_result = poller.poll_pending_operations(pending_ops, "create")
            created_databases += poll_result.successes
            failed_folders += poll_result.failures

    if args.dry_run:
        logger.info(
            f"YDB dedicated dry run completed. Planned {planned_databases} "
            f"databases, skipped {skipped_folders} folders"
        )
    else:
        logger.info(
            f"YDB creation completed. Created {created_databases} databases, skipped {skipped_folders} folders"
        )
    if failed_folders:
        raise UserCreationError(f"Dedicated YDB provisioning failed in {failed_folders} folder(s)")


def _validate_serverless_settings(args) -> None:
    """Validate user-controlled Serverless cost and capacity limits."""
    if args.serverless_storage_size_limit_gb <= 0:
        raise ValidationError("Serverless storage size limit must be greater than zero")
    if args.serverless_throttling_rcu_limit < 0:
        raise ValidationError("Serverless throttling RCU limit cannot be negative")
    if args.serverless_provisioned_rcu_limit < 0:
        raise ValidationError("Serverless provisioned RCU limit cannot be negative")
    if args.serverless_enable_throttling and args.serverless_throttling_rcu_limit == 0:
        raise ValidationError("Enabled Serverless throttling requires a non-zero RCU limit")


def run_serverless_ydb_mode(args, user_creator: UserCreator) -> None:
    """Create one Serverless YDB database per selected folder."""
    validate_cloud_id(args.cloud_id)
    validate_workshop_prefix(args.workshop_prefix)
    _validate_serverless_settings(args)
    skip_folder_ids = parse_skip_folder_ids(args.skip_folder_ids)

    target_folders = _resolve_ydb_target_folders(args, user_creator)

    created_databases = 0
    planned_databases = 0
    skipped_folders = 0
    failed_folders = 0
    pending_ops = []
    poller = OperationPoller(user_creator)

    with OperationTimer("Serverless YDB creation"):
        for folder in target_folders:
            folder_id = folder["id"]
            folder_name = folder["name"]
            database_name = _ydb_database_name(folder_name, args.workshop_prefix)

            if folder_id in skip_folder_ids:
                skipped_folders += 1
                continue

            try:
                existing_dbs = user_creator.list_ydb_databases_in_folder(folder_id)
                if any(is_serverless_ydb(db) for db in existing_dbs):
                    logger.info(
                        f"Folder {folder_name} already has a Serverless YDB database. Skipping."
                    )
                    skipped_folders += 1
                    continue
                if any(db.get("name") == database_name for db in existing_dbs):
                    logger.warning(
                        f"Folder {folder_name} already has a database named "
                        f"{database_name}. Skipping to avoid a name conflict."
                    )
                    skipped_folders += 1
                    continue

                if args.dry_run:
                    logger.info(
                        f"DRY RUN: would create Serverless YDB database "
                        f"{database_name} in folder {folder_name}"
                    )
                    planned_databases += 1
                    continue

                while len(pending_ops) >= Constants.MAX_CONCURRENT_OPERATIONS:
                    poll_result = poller.poll_pending_operations(pending_ops, "create")
                    created_databases += poll_result.successes
                    failed_folders += poll_result.failures

                operation_id = user_creator.start_serverless_ydb_database(
                    folder_id=folder_id,
                    database_name=database_name,
                    description=f"Serverless YDB database for folder {folder_name}",
                    storage_size_limit_gb=args.serverless_storage_size_limit_gb,
                    enable_throttling_rcu_limit=args.serverless_enable_throttling,
                    throttling_rcu_limit=args.serverless_throttling_rcu_limit,
                    provisioned_rcu_limit=args.serverless_provisioned_rcu_limit,
                )
                pending_ops.append(
                    {
                        "folder_id": folder_id,
                        "folder_name": folder_name,
                        "operation_id": operation_id,
                        "start_time": time.time(),
                    }
                )
            except UserCreationError as exc:
                logger.error(
                    f"Failed to start Serverless YDB for folder {folder_name} "
                    f"(ID: {folder_id}): {exc}"
                )
                failed_folders += 1

        while pending_ops:
            poll_result = poller.poll_pending_operations(pending_ops, "create")
            created_databases += poll_result.successes
            failed_folders += poll_result.failures

    if args.dry_run:
        logger.info(
            f"Serverless YDB dry run completed. Planned {planned_databases} "
            f"databases, skipped {skipped_folders} folders"
        )
    else:
        logger.info(
            f"Serverless YDB creation completed. Created {created_databases} "
            f"databases, skipped {skipped_folders} folders"
        )
    if failed_folders:
        raise UserCreationError(f"Serverless YDB provisioning failed in {failed_folders} folder(s)")


def run_delete_ydb_mode(args, user_creator: UserCreator) -> None:
    """Delete YDB databases only in explicitly selected folders."""
    validate_cloud_id(args.cloud_id)

    if not getattr(args, "folder_ids", None):
        raise ValidationError(
            "--folder-ids is required for delete-ydb; refusing to target the whole cloud"
        )
    if not args.dry_run and not args.confirm_delete_ydb:
        raise ValidationError(
            "Live YDB deletion requires YC_CONFIRM_DELETE_YDB=true or --confirm-delete-ydb"
        )

    logger.info(f"Starting YDB deletion mode for cloud {args.cloud_id}")

    folder_ids = parse_comma_separated_ids(args.folder_ids)
    if not folder_ids:
        raise ValidationError("--folder-ids must contain at least one folder ID")
    folders = [user_creator.get_folder(folder_id) for folder_id in folder_ids]
    foreign = [
        folder.get("id", "")
        for folder in folders
        if folder.get("cloudId") and folder.get("cloudId") != args.cloud_id
    ]
    if foreign:
        raise ValidationError("Selected folders belong to another cloud: " + ", ".join(foreign))
    logger.info(f"delete-ydb: using provided folder IDs: {folder_ids}")

    # Build skip set
    skip_set = parse_skip_folder_ids(getattr(args, "skip_folder_ids", None))

    deleted_databases = 0
    failed_operations = 0
    pending_ops = []  # list of dicts: {folder_id, folder_name, database_id, operation_id}
    poller = OperationPoller(user_creator)

    # Collect all databases to delete
    databases_to_delete = []
    for folder in folders:
        folder_id = folder["id"]
        folder_name = folder.get("name", folder_id)
        if folder_id in skip_set:
            logger.info(f"delete-ydb: skipping folder {folder_name} (ID: {folder_id})")
            continue
        try:
            # List YDB databases in the folder
            databases = user_creator.list_ydb_databases_in_folder(folder_id)
            logger.info(
                f"Found {len(databases)} YDB databases in folder {folder_name} (ID: {folder_id})"
            )

            for db in databases:
                databases_to_delete.append(
                    {
                        "folder_id": folder_id,
                        "folder_name": folder_name,
                        "database_id": db["id"],
                        "database_name": db.get("name", db["id"]),
                    }
                )

        except UserCreationError as e:
            logger.error(f"Failed to list YDB databases in folder {folder_id}: {e}")
            failed_operations += 1
            continue

    logger.info(f"Total databases to delete: {len(databases_to_delete)}")

    if args.dry_run:
        for db_info in databases_to_delete:
            logger.info(
                "DRY RUN: would delete YDB database %s (%s) from folder %s",
                db_info["database_name"],
                db_info["database_id"],
                db_info["folder_name"],
            )
        if failed_operations:
            raise UserCreationError(
                f"YDB deletion preflight failed in {failed_operations} folder(s)"
            )
        logger.info(f"YDB deletion dry run completed. Planned {len(databases_to_delete)} databases")
        return

    with OperationTimer("YDB deletion"):
        # Start deletion operations with concurrency control
        for db_info in databases_to_delete:
            folder_id = db_info["folder_id"]
            folder_name = db_info["folder_name"]
            database_id = db_info["database_id"]
            database_name = db_info["database_name"]

            try:
                # Flow control: if we already have max concurrent ops in-flight, poll until one finishes
                while len(pending_ops) >= Constants.MAX_CONCURRENT_OPERATIONS:
                    poll_result = poller.poll_pending_operations(pending_ops, "delete")
                    deleted_databases += poll_result.successes
                    failed_operations += poll_result.failures

                # Start YDB delete operation (non-blocking)
                op_id = user_creator.start_ydb_database_deletion(database_id)

                pending_ops.append(
                    {
                        "folder_id": folder_id,
                        "folder_name": folder_name,
                        "database_id": database_id,
                        "database_name": database_name,
                        "operation_id": op_id,
                        "start_time": time.time(),
                    }
                )

            except UserCreationError as e:
                logger.error(
                    f"Failed to start YDB deletion for database {database_name} in folder {folder_name} (ID: {folder_id}): {e}"
                )
                failed_operations += 1
                continue

        # Finalize any remaining operations
        while pending_ops:
            poll_result = poller.poll_pending_operations(pending_ops, "delete")
            deleted_databases += poll_result.successes
            failed_operations += poll_result.failures

    logger.info(f"YDB deletion completed. Deleted {deleted_databases} databases")
    if failed_operations:
        raise UserCreationError(f"YDB deletion failed for {failed_operations} operation(s)")


def run_reset_password_mode(args, user_creator: UserCreator) -> None:
    """Run password reset mode with improved utilities and error handling."""
    # Validate inputs
    if not args.userpool_id:
        raise ValidationError("--userpool-id is required for reset-password mode")

    validate_userpool_id(args.userpool_id)
    validate_created_users_file(
        args.created_users_file,
        overwrite=args.overwrite_output,
    )

    # Build username map by listing users (used for output consistency)
    logger.info(f"Listing users from userpool {args.userpool_id} to build username map")
    users = user_creator.list_users_in_userpool(args.userpool_id)
    username_by_id = {u["id"]: u.get("username", "") for u in users}

    # Decide target users
    if getattr(args, "user_ids", None):
        target_user_ids = parse_comma_separated_ids(args.user_ids)
        logger.info(f"Resetting password for provided {len(target_user_ids)} user(s)")
    else:
        target_user_ids = list(username_by_id.keys())
        logger.info(f"Collected {len(target_user_ids)} user(s) to reset from userpool")

    successes = 0
    failures = 0

    with OperationTimer("password reset"):
        with safe_file_writer(args.created_users_file) as f:
            f.write("id,username,password\n")
            f.flush()

            for i, user_id in enumerate(target_user_ids):
                try:
                    log_operation_progress(i, len(target_user_ids), "Password reset")

                    # Generate a new password
                    password, generation_proof = user_creator.generate_password(
                        args.password_length
                    )
                    # Set password for the user
                    user_creator.set_others_password(user_id, password, generation_proof)

                    username = username_by_id.get(user_id, "")
                    f.write(f"{user_id},{username},{password}\n")
                    f.flush()

                    successes += 1
                except UserCreationError as e:
                    logger.error(f"Failed to reset password for user {user_id}: {e}")
                    failures += 1

    logger.info(f"Password reset completed. Success: {successes}, Failed: {failures}")
    if failures:
        raise UserCreationError(f"Password reset failed for {failures} user(s)")


def run_generate_load_mode(args, user_creator: UserCreator) -> None:
    """Run load generation mode with improved utilities and error handling."""
    # Validate inputs
    validate_cloud_id(args.cloud_id)
    validate_batch_size(args.batch_size)
    validate_output_directory(args.output_dir)

    # Resolve folders to process
    if getattr(args, "folder_ids", None):
        folder_ids = parse_comma_separated_ids(args.folder_ids)
        folders = create_folder_objects_from_ids(folder_ids)
        logger.info(f"generate-load: using provided folder IDs: {folder_ids}")
    else:
        folders = user_creator.list_folders(args.cloud_id)
        logger.info(f"generate-load: listed {len(folders)} folders in cloud {args.cloud_id}")

    # Build skip set
    skip_set = parse_skip_folder_ids(getattr(args, "skip_folder_ids", None))

    # Open init script; mixed/select will be split into batch files
    init_path = os.path.join(args.output_dir, "init.bash")

    generated = 0
    batch_size = args.batch_size
    mixed_f = None
    batch_no = 1
    current_batch_count = 0
    batch_files = []

    with OperationTimer("load script generation"):
        with safe_file_writer(init_path) as init_f:
            init_f.write("#!/usr/bin/env bash\n")

            for folder in folders:
                folder_id = folder["id"]
                folder_name = folder.get("name", folder_id)
                if folder_id in skip_set:
                    logger.info(f"generate-load: skipping folder {folder_name} (ID: {folder_id})")
                    continue

                # Find first YDB with storage groups
                try:
                    dbs = user_creator.list_ydb_databases_in_folder(folder_id)
                except UserCreationError as e:
                    logger.error(f"generate-load: failed to list YDB in folder {folder_id}: {e}")
                    continue

                target_db = None
                for db in dbs:
                    if has_ydb_storage_groups(db):
                        target_db = db
                        break

                if not target_db:
                    logger.info(
                        f"generate-load: no YDB with storage groups found in folder {folder_id}"
                    )
                    continue

                db_id = target_db["id"]
                endpoint = target_db.get("endpoint", "")

                # Generate commands
                init_cmd = (
                    f"ydb --use-metadata-credentials -e {endpoint} -d /ru-central1/{args.cloud_id}/{db_id} "
                    f"workload kv init --auto-partition 0 --max-partitions 1 --min-partitions 1 > init-{db_id} 2>&1"
                )
                mixed_cmd = (
                    f"ydb --use-metadata-credentials -e {endpoint} -d /ru-central1/{args.cloud_id}/{db_id} "
                    f"workload kv run mixed -t 100 --seconds {Constants.LOAD_DURATION_SECONDS} > mixed-{db_id} 2>&1 &"
                )
                select_cmd = (
                    f"ydb --use-metadata-credentials -e {endpoint} -d /ru-central1/{args.cloud_id}/{db_id} "
                    f"workload kv run select --threads 10 --seconds {Constants.LOAD_DURATION_SECONDS} --rows 1000 > select-{db_id} 2>&1 &"
                )

                init_f.write(init_cmd + "\n")

                # Rotate batch file if needed
                if mixed_f is None or current_batch_count >= batch_size:
                    # close previous batch file
                    if mixed_f is not None:
                        mixed_f.close()
                    mixed_path = os.path.join(
                        args.output_dir, f"run-mixed-and-select-{batch_no}.bash"
                    )
                    mixed_f = open(mixed_path, "w")
                    mixed_f.write("#!/usr/bin/env bash\n")
                    batch_files.append(mixed_path)
                    batch_no += 1
                    current_batch_count = 0

                mixed_f.write(mixed_cmd + "\n")
                mixed_f.write(select_cmd + "\n")
                generated += 1
                current_batch_count += 1

        # Close the last batch file
        if mixed_f is not None:
            mixed_f.close()

        # Make executable
        try:
            os.chmod(init_path, 0o755)
            for path in batch_files:
                try:
                    os.chmod(path, 0o755)
                except Exception as e:
                    logger.warning(f"Failed to make script executable: {path}: {e}")
        except Exception as e:
            logger.warning(f"Failed to make scripts executable: {e}")

    logger.info(
        f"generate-load: wrote init.bash and {len(batch_files)} mixed/select batch script(s) to {args.output_dir}. Databases targeted: {generated}"
    )
