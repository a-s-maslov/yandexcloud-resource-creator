import unittest

from resource_api import (
    DatabaseTarget,
    ServerlessDatabaseManager,
    ServerlessSettings,
)


class FakeClient:
    def __init__(self):
        self.databases = {}
        self.started = []
        self.deleted = []

    def get_folder(self, folder_id):
        return {"id": folder_id, "name": f"folder-{folder_id}", "cloudId": "cloud-1"}

    def list_ydb_databases_in_folder(self, folder_id):
        item = self.databases.get(folder_id)
        return [] if item is None else [item]

    def get_ydb_database(self, database_id):
        return next(item for item in self.databases.values() if item["id"] == database_id)

    def start_serverless_ydb_database(self, folder_id, database_name, **_settings):
        self.started.append((folder_id, database_name))
        self.databases[folder_id] = {
            "id": f"db-{folder_id}",
            "folderId": folder_id,
            "name": database_name,
            "status": "RUNNING",
            "endpoint": f"grpcs://endpoint.example.net:2135/?database=/cloud-1/{folder_id}/db",
            "serverlessDatabase": {},
        }
        return f"op-{folder_id}"

    def start_ydb_database_deletion(self, database_id):
        self.deleted.append(database_id)
        for folder_id, item in list(self.databases.items()):
            if item["id"] == database_id:
                del self.databases[folder_id]
        return f"delete-{database_id}"

    def get_operation_status(self, _operation_id):
        return {"done": True}


class ServerlessDatabaseManagerTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.manager = ServerlessDatabaseManager(self.client, "cloud-1", max_concurrent=2)

    def test_ensure_is_idempotent_and_returns_connection_details(self):
        targets = [DatabaseTarget("f1", "folder-f1", "workshop-db")]
        created = self.manager.ensure(targets, ServerlessSettings())
        existing = self.manager.ensure(targets, ServerlessSettings())

        self.assertEqual(self.client.started, [("f1", "workshop-db")])
        self.assertEqual(created, existing)
        self.assertEqual(created[0].database_path, "/cloud-1/f1/db")

    def test_resolve_database_ids_rejects_neither_folder_nor_type(self):
        targets = [DatabaseTarget("f1", "folder-f1", "workshop-db")]
        database = self.manager.ensure(targets, ServerlessSettings())[0]
        resolved = self.manager.resolve_database_ids([database.database_id])
        self.assertEqual(resolved[0].folder_id, "f1")

    def test_delete_only_selected_database(self):
        targets = [
            DatabaseTarget("f1", "folder-f1", "workshop-1"),
            DatabaseTarget("f2", "folder-f2", "workshop-2"),
        ]
        self.manager.ensure(targets, ServerlessSettings())
        self.manager.delete(targets[:1])
        self.assertEqual(self.client.deleted, ["db-f1"])
        self.assertEqual(len(self.manager.inventory(targets)), 1)


if __name__ == "__main__":
    unittest.main()
