"""Reusable API for managing explicitly selected Serverless YDB databases."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, urlsplit

from exceptions import UserCreationError, ValidationError
from operation_poller import OperationPoller
from user_creator import UserCreator
from utils import is_serverless_ydb


@dataclass(frozen=True)
class ServerlessSettings:
    storage_size_limit_gb: int = 50
    enable_throttling_rcu_limit: bool = True
    throttling_rcu_limit: int = 10
    provisioned_rcu_limit: int = 0


@dataclass(frozen=True)
class DatabaseTarget:
    folder_id: str
    folder_name: str
    database_name: str


@dataclass(frozen=True)
class ServerlessDatabase:
    folder_id: str
    folder_name: str
    database_id: str
    database_name: str
    endpoint: str
    database_path: str
    status: str


class ServerlessDatabaseManager:
    """Manage Serverless YDBs without exposing CLI-specific argument objects."""

    def __init__(self, client: UserCreator, cloud_id: str, max_concurrent: int = 5):
        if not cloud_id:
            raise ValidationError("cloud_id is required")
        if max_concurrent < 1:
            raise ValidationError("max_concurrent must be positive")
        self.client = client
        self.cloud_id = cloud_id
        self.max_concurrent = max_concurrent

    def resolve_targets(
        self, folder_ids: Iterable[str], database_name_for_folder
    ) -> list[DatabaseTarget]:
        targets = []
        seen = set()
        for folder_id in folder_ids:
            if folder_id in seen:
                continue
            seen.add(folder_id)
            folder = self.client.get_folder(folder_id)
            if folder.get("cloudId") and folder["cloudId"] != self.cloud_id:
                raise ValidationError(f"Folder {folder_id} belongs to another cloud")
            folder_name = folder.get("name", folder_id)
            targets.append(
                DatabaseTarget(folder_id, folder_name, database_name_for_folder(folder_name))
            )
        if not targets:
            raise ValidationError("At least one folder ID is required")
        return targets

    def inventory(self, targets: Iterable[DatabaseTarget]) -> list[ServerlessDatabase]:
        result = []
        for target in targets:
            matches = [
                item
                for item in self.client.list_ydb_databases_in_folder(target.folder_id)
                if is_serverless_ydb(item)
            ]
            if len(matches) > 1:
                raise UserCreationError(
                    f"Folder {target.folder_name} contains {len(matches)} Serverless databases"
                )
            if matches:
                result.append(self._database(target, matches[0]))
        return result

    def resolve_database_ids(self, database_ids: Iterable[str]) -> list[ServerlessDatabase]:
        result = []
        seen = set()
        for database_id in database_ids:
            if database_id in seen:
                continue
            seen.add(database_id)
            item = self.client.get_ydb_database(database_id)
            folder_id = item.get("folderId", "")
            if not folder_id:
                raise UserCreationError(f"Database {database_id} has no folderId")
            folder = self.client.get_folder(folder_id)
            if folder.get("cloudId") and folder["cloudId"] != self.cloud_id:
                raise ValidationError(f"Database {database_id} belongs to another cloud")
            if not is_serverless_ydb(item):
                raise ValidationError(f"Database {database_id} is not Serverless")
            target = DatabaseTarget(
                folder_id=folder_id,
                folder_name=folder.get("name", folder_id),
                database_name=item.get("name", database_id),
            )
            result.append(self._database(target, item))
        if not result:
            raise ValidationError("At least one database ID is required")
        return result

    def ensure(
        self,
        targets: Iterable[DatabaseTarget],
        settings: ServerlessSettings,
    ) -> list[ServerlessDatabase]:
        targets = list(targets)
        existing = {item.folder_id: item for item in self.inventory(targets)}
        pending = []
        poller = OperationPoller(self.client)
        failures = 0
        for target in targets:
            if target.folder_id in existing:
                continue
            while len(pending) >= self.max_concurrent:
                polled = poller.poll_pending_operations(pending, "create")
                failures += polled.failures
            operation_id = self.client.start_serverless_ydb_database(
                folder_id=target.folder_id,
                database_name=target.database_name,
                description=f"Serverless YDB database for folder {target.folder_name}",
                storage_size_limit_gb=settings.storage_size_limit_gb,
                enable_throttling_rcu_limit=settings.enable_throttling_rcu_limit,
                throttling_rcu_limit=settings.throttling_rcu_limit,
                provisioned_rcu_limit=settings.provisioned_rcu_limit,
            )
            pending.append(
                {
                    "folder_id": target.folder_id,
                    "folder_name": target.folder_name,
                    "operation_id": operation_id,
                    "start_time": time.time(),
                }
            )
        while pending:
            polled = poller.poll_pending_operations(pending, "create")
            failures += polled.failures
        if failures:
            raise UserCreationError(f"Creation failed for {failures} Serverless database(s)")
        databases = self.inventory(targets)
        if len(databases) != len(targets):
            raise UserCreationError(
                f"Expected {len(targets)} Serverless databases, found {len(databases)}"
            )
        return databases

    def delete(self, targets: Iterable[DatabaseTarget]) -> None:
        databases = self.inventory(targets)
        pending = []
        failures = 0
        poller = OperationPoller(self.client)
        for database in databases:
            while len(pending) >= self.max_concurrent:
                polled = poller.poll_pending_operations(pending, "delete")
                failures += polled.failures
            operation_id = self.client.start_ydb_database_deletion(database.database_id)
            pending.append(
                {
                    "folder_id": database.folder_id,
                    "folder_name": database.folder_name,
                    "database_id": database.database_id,
                    "database_name": database.database_name,
                    "operation_id": operation_id,
                    "start_time": time.time(),
                }
            )
        while pending:
            polled = poller.poll_pending_operations(pending, "delete")
            failures += polled.failures
        if failures:
            raise UserCreationError(f"Deletion failed for {failures} Serverless database(s)")

    @staticmethod
    def _database(target: DatabaseTarget, item: dict) -> ServerlessDatabase:
        connection = urlsplit(item.get("endpoint", ""))
        paths = parse_qs(connection.query).get("database", [])
        if not connection.scheme or not connection.netloc or len(paths) != 1:
            raise UserCreationError(
                f"Database {item.get('id', '<unknown>')} has an unexpected endpoint"
            )
        return ServerlessDatabase(
            folder_id=target.folder_id,
            folder_name=target.folder_name,
            database_id=item["id"],
            database_name=item.get("name", item["id"]),
            endpoint=f"{connection.scheme}://{connection.netloc}",
            database_path=paths[0],
            status=item.get("status", "UNKNOWN"),
        )
