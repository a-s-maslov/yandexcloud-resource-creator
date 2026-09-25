import csv
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from auth import IAM_TOKEN_AUDIENCE, create_service_account_iam_token
from config import EnvironmentConfig, env_bool, env_int
from exceptions import ConfigurationError, UserCreationError, ValidationError
from main import create_argument_parser
from modes import (
    build_workshop_user_specs,
    preflight_workshop_user_batch,
    run_create_folders_mode,
    run_delete_ydb_mode,
    run_generate_load_mode,
    run_reset_password_mode,
    run_serverless_ydb_mode,
    run_users_mode,
    run_ydb_mode,
)
from operation_poller import OperationPoller
from user_creator import UserCreator
from utils import has_dedicated_ydb_storage, is_serverless_ydb


class FakeCreator:
    def __init__(self, users=None, folders=None, userpools=None):
        self.users = [] if users is None else users
        self.folders = [] if folders is None else folders
        self.userpools = (
            [
                {
                    "id": "pool123",
                    "organizationId": "org123",
                    "name": "scale-2026-workshop",
                    "domains": ["workshop.idp.yandexcloud.net"],
                    "status": "ACTIVE",
                }
            ]
            if userpools is None
            else userpools
        )
        self.created = []
        self.created_pools = []
        self.folder_grants = []
        self.cloud_grants = []
        self.password_hash_updates = []
        self.deleted_users = []
        self.created_folders = []

    def get_cloud(self, cloud_id):
        return {"id": cloud_id, "organizationId": "org123"}

    def get_userpool(self, userpool_id):
        return next(pool for pool in self.userpools if pool["id"] == userpool_id)

    def list_userpools(self, _organization_id):
        return self.userpools

    def create_userpool(self, organization_id, name, default_subdomain):
        self.created_pools.append(
            {
                "organization_id": organization_id,
                "name": name,
                "default_subdomain": default_subdomain,
            }
        )
        return {
            "id": "newpool123",
            "organizationId": organization_id,
            "name": name,
            "domains": [f"{default_subdomain}.idp.yandexcloud.net"],
            "status": "ACTIVE",
        }

    def list_users_in_userpool(self, _userpool_id):
        return self.users

    def list_folders(self, _cloud_id):
        return self.folders

    def generate_password(self, length=11):
        return "Abcdefghi23"[:length], None

    def create_user(self, **kwargs):
        self.created.append(kwargs)
        return f"user-{len(self.created)}"

    def set_user_password_hash(self, **kwargs):
        self.password_hash_updates.append(kwargs)

    def delete_user(self, user_id):
        self.deleted_users.append(user_id)

    def create_folder(self, cloud_id, folder_name, description):
        self.created_folders.append(
            {
                "cloud_id": cloud_id,
                "folder_name": folder_name,
                "description": description,
            }
        )
        return f"folder-{folder_name}"

    def grant_folder_access(self, **kwargs):
        self.folder_grants.append(kwargs)

    def grant_cloud_access(self, **kwargs):
        self.cloud_grants.append(kwargs)


class PartialCreator(FakeCreator):
    def grant_folder_access(self, **kwargs):
        raise UserCreationError("role grant failed")


class PasswordHashFailureCreator(FakeCreator):
    def set_user_password_hash(self, **kwargs):
        raise UserCreationError("hash import denied")


class FolderQuotaFailureCreator(FakeCreator):
    def create_folder(self, cloud_id, folder_name, description):
        self.created_folders.append(folder_name)
        raise UserCreationError("Folder creation failed: 429 Client Error: quota exceeded")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def post(self, url, json=None):
        self.calls.append((url, json))
        return FakeResponse({"id": "operation-1"})

    def delete(self, url, params=None):
        self.calls.append((url, params))
        return FakeResponse({"id": "operation-delete-1"})


class FakeTokenSession:
    def __init__(self, payload=None):
        self.payload = payload or {"iamToken": "service-account-token"}
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        return FakeResponse(self.payload)


class FakeYdbCreator:
    def __init__(self, databases=None, network_id=None, subnet_ids=None):
        self.databases = [] if databases is None else databases
        self.network_id = network_id
        self.subnet_ids = [] if subnet_ids is None else subnet_ids
        self.vpc_creations = []
        self.dedicated_creations = []
        self.serverless_creations = []
        self.database_deletions = []

    def list_folders(self, _cloud_id):
        return [{"id": "folder123", "name": "scale-2026-f-001"}]

    def get_folder(self, folder_id):
        return {
            "id": folder_id,
            "name": "scale-2026-f-001",
            "cloudId": "cloud123",
        }

    def list_ydb_databases_in_folder(self, _folder_id):
        return self.databases

    def check_existing_vpc(self, _folder_id):
        return self.network_id, self.subnet_ids

    def create_vpc_with_subnets(self, **kwargs):
        self.vpc_creations.append(kwargs)
        return "network-new", ["subnet-a", "subnet-b", "subnet-d"]

    def start_ydb_database(self, **kwargs):
        self.dedicated_creations.append(kwargs)
        return "dedicated-op"

    def start_serverless_ydb_database(self, **kwargs):
        self.serverless_creations.append(kwargs)
        return "serverless-op"

    def start_ydb_database_deletion(self, database_id):
        self.database_deletions.append(database_id)
        return "delete-op"

    def get_operation_status(self, _operation_id):
        return {"done": True}


class FailingYdbListCreator(FakeYdbCreator):
    def list_ydb_databases_in_folder(self, _folder_id):
        raise UserCreationError("permission denied")


class FailedOperationCreator(FakeYdbCreator):
    def get_operation_status(self, _operation_id):
        return {
            "done": True,
            "error": {"code": 13, "message": "quota exceeded", "details": []},
        }


class FailingPasswordResetCreator(FakeCreator):
    def set_others_password(self, user_id, password, generation_proof=None):
        raise UserCreationError("password reset denied")


class UserProvisioningTests(unittest.TestCase):
    def make_args(self, output_file, dry_run=False):
        return SimpleNamespace(
            userpool_id="pool123",
            userpool_name="scale-2026-workshop",
            userpool_subdomain="scale-2026-workshop",
            create_userpool=True,
            num_users=2,
            domain="workshop.idp.yandexcloud.net",
            cloud_id="cloud123",
            workshop_prefix="scale-2026",
            start_index=1,
            password_length=11,
            require_password_change=False,
            folder_role="ydb.editor",
            user_expires_at="2099-09-30T21:00:00Z",
            dry_run=dry_run,
            created_users_file=output_file,
            resource_manifest_file=os.path.join(
                os.path.dirname(output_file), "workshop_resources.csv"
            ),
            overwrite_output=False,
        )

    def test_specs_are_deterministic(self):
        specs = build_workshop_user_specs(2, 7, "scale-2026", "example.net")
        self.assertEqual(specs[0].username, "scale-2026_007@example.net")
        self.assertEqual(specs[1].folder_name, "scale-2026-f-008")
        self.assertEqual(specs[0].full_name, "Workshop User 007")

    def test_preflight_rejects_existing_targets(self):
        specs = build_workshop_user_specs(1, 1, "scale", "example.net")
        creator = FakeCreator(users=[{"username": "scale_001@example.net"}])
        with self.assertRaises(ValidationError):
            preflight_workshop_user_batch(specs, "pool123", "cloud123", creator)

    def test_dry_run_does_not_create_resources_or_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = os.path.join(temp_dir, "created_users.csv")
            creator = FakeCreator()
            run_users_mode(self.make_args(output_file, dry_run=True), creator)
            self.assertEqual(creator.created, [])
            self.assertFalse(os.path.exists(output_file))

    def test_dry_run_plans_missing_userpool_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = os.path.join(temp_dir, "created_users.csv")
            creator = FakeCreator(userpools=[])
            args = self.make_args(output_file, dry_run=True)
            args.userpool_id = None
            args.domain = None

            run_users_mode(args, creator)

            self.assertEqual(creator.created_pools, [])
            self.assertEqual(creator.created, [])
            self.assertFalse(os.path.exists(output_file))

    def test_missing_userpool_is_created_before_users(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = os.path.join(temp_dir, "created_users.csv")
            creator = FakeCreator(userpools=[])
            args = self.make_args(output_file)
            args.userpool_id = None
            args.domain = None

            run_users_mode(args, creator)

            self.assertEqual(len(creator.created_pools), 1)
            self.assertEqual(creator.created[0]["userpool_id"], "newpool123")
            self.assertEqual(
                creator.created[0]["username"],
                "scale-2026_001@scale-2026-workshop.idp.yandexcloud.net",
            )

    def test_successful_batch_writes_complete_csv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = os.path.join(temp_dir, "created_users.csv")
            creator = FakeCreator()
            run_users_mode(self.make_args(output_file), creator)

            with open(output_file, encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["username"], "scale-2026_001@workshop.idp.yandexcloud.net")
            self.assertEqual(rows[0]["folder_id"], "folder-scale-2026-f-001")
            self.assertEqual(rows[0]["status"], "ready")
            self.assertEqual(rows[0]["password"], "Abcdefghi23")
            self.assertEqual(
                creator.created[0]["expires_at"],
                "2099-09-30T21:00:00Z",
            )
            self.assertEqual(len(creator.folder_grants), 2)
            self.assertEqual(creator.folder_grants[0]["role_id"], "ydb.editor")
            self.assertEqual(len(creator.cloud_grants), 2)
            self.assertEqual(len(creator.password_hash_updates), 2)
            self.assertEqual(
                creator.password_hash_updates[0],
                {
                    "user_id": "user-1",
                    "password": "Abcdefghi23",
                    "need_change": False,
                },
            )

    def test_partial_batch_is_recorded_and_fails_the_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = os.path.join(temp_dir, "created_users.csv")
            creator = PartialCreator()
            with self.assertRaises(UserCreationError):
                run_users_mode(self.make_args(output_file), creator)

            with open(output_file, encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], "partial")
            self.assertEqual(rows[0]["error"], "role grant failed")

    def test_permanent_password_failure_rolls_back_and_stops_batch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = os.path.join(temp_dir, "created_users.csv")
            creator = PasswordHashFailureCreator()

            with self.assertRaises(UserCreationError):
                run_users_mode(self.make_args(output_file), creator)

            self.assertEqual(len(creator.created), 1)
            self.assertEqual(creator.deleted_users, ["user-1"])
            with open(output_file, encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["user_id"], "")

    def test_create_folders_reuses_existing_users_without_modifying_them(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            args = self.make_args(os.path.join(temp_dir, "credentials.csv"))
            creator = FakeCreator(
                users=[
                    {
                        "id": "existing-user-1",
                        "username": "scale-2026_001@workshop.idp.yandexcloud.net",
                    },
                    {
                        "id": "existing-user-2",
                        "username": "scale-2026_002@workshop.idp.yandexcloud.net",
                    },
                ]
            )

            run_create_folders_mode(args, creator)

            self.assertEqual(creator.created, [])
            self.assertEqual(creator.password_hash_updates, [])
            self.assertEqual(
                [item["folder_name"] for item in creator.created_folders],
                ["scale-2026-f-001", "scale-2026-f-002"],
            )
            with open(args.resource_manifest_file, encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["user_id"], "existing-user-1")
            self.assertEqual(rows[0]["database_name"], "scale-2026-db-001")
            self.assertNotIn("password", rows[0])

    def test_create_folders_fails_before_mutation_when_user_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            args = self.make_args(os.path.join(temp_dir, "credentials.csv"))
            creator = FakeCreator(users=[])

            with self.assertRaises(ValidationError):
                run_create_folders_mode(args, creator)

            self.assertEqual(creator.created_folders, [])
            self.assertEqual(creator.folder_grants, [])

    def test_create_folders_dry_run_never_mutates_users_or_folders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            args = self.make_args(os.path.join(temp_dir, "credentials.csv"), dry_run=True)
            creator = FakeCreator(
                users=[
                    {
                        "id": f"existing-user-{index}",
                        "username": (f"scale-2026_{index:03d}@workshop.idp.yandexcloud.net"),
                    }
                    for index in (1, 2)
                ]
            )

            run_create_folders_mode(args, creator)

            self.assertEqual(creator.created, [])
            self.assertEqual(creator.created_folders, [])
            self.assertEqual(creator.folder_grants, [])
            self.assertEqual(creator.cloud_grants, [])
            self.assertFalse(os.path.exists(args.resource_manifest_file))

    def test_create_folders_stops_after_first_quota_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            args = self.make_args(os.path.join(temp_dir, "credentials.csv"))
            creator = FolderQuotaFailureCreator(
                users=[
                    {
                        "id": f"existing-user-{index}",
                        "username": (f"scale-2026_{index:03d}@workshop.idp.yandexcloud.net"),
                    }
                    for index in (1, 2)
                ]
            )

            with self.assertRaisesRegex(UserCreationError, "stopped"):
                run_create_folders_mode(args, creator)

            self.assertEqual(creator.created_folders, ["scale-2026-f-001"])


class AccessBindingTests(unittest.TestCase):
    def test_folder_deletion_passes_explicit_timestamp(self):
        session = FakeSession()
        creator = UserCreator("token", session=session)

        operation_id = creator.start_folder_deletion(
            "folder-1",
            "2026-09-14T11:00:00Z",
        )

        url, params = session.calls[0]
        self.assertTrue(url.endswith("/folders/folder-1"))
        self.assertEqual(params["deleteAfter"], "2026-09-14T11:00:00Z")
        self.assertEqual(operation_id, "operation-delete-1")

    def test_local_password_is_short_copy_friendly_and_has_three_classes(self):
        creator = UserCreator("token", session=FakeSession())
        password, generation_proof = creator.generate_password(11)

        self.assertEqual(len(password), 11)
        self.assertNotIn("-", password)
        self.assertTrue(any(character.islower() for character in password))
        self.assertTrue(any(character.isupper() for character in password))
        self.assertTrue(any(character.isdigit() for character in password))
        self.assertIsNone(generation_proof)

    def test_folder_role_is_added_without_replacing_other_bindings(self):
        session = FakeSession()
        creator = UserCreator("token", session=session)
        creator.poll_operation = lambda operation_id, description: {}

        creator.grant_folder_access("folder-1", "user-1", "editor")

        url, payload = session.calls[0]
        self.assertTrue(url.endswith("/folder-1:updateAccessBindings"))
        self.assertEqual(payload["accessBindingDeltas"][0]["action"], "ADD")
        binding = payload["accessBindingDeltas"][0]["accessBinding"]
        self.assertEqual(binding["roleId"], "editor")
        self.assertEqual(binding["subject"]["id"], "user-1")
        self.assertEqual(binding["subject"]["type"], "userAccount")

    def test_folder_role_supports_service_account_subject(self):
        session = FakeSession()
        creator = UserCreator("token", session=session)
        creator.poll_operation = lambda operation_id, description: {}

        creator.update_folder_access(
            "folder-1",
            "service-account-1",
            "vpc.privateAdmin",
            "ADD",
            subject_type="serviceAccount",
        )

        _, payload = session.calls[0]
        binding = payload["accessBindingDeltas"][0]["accessBinding"]
        self.assertEqual(binding["roleId"], "vpc.privateAdmin")
        self.assertEqual(binding["subject"], {"id": "service-account-1", "type": "serviceAccount"})

    def test_ad_md4_password_hash_matches_known_nt_hash(self):
        self.assertEqual(
            UserCreator._ad_md4_password_hash("password"),
            "8846F7EAEE8FB117AD06BDD830B7586C",
        )

    def test_permanent_password_hash_disables_first_login_change(self):
        session = FakeSession()
        creator = UserCreator("token", session=session)
        creator.poll_operation = lambda operation_id, description: {}

        creator.set_user_password_hash("user-1", "password", need_change=False)

        url, payload = session.calls[0]
        self.assertTrue(url.endswith("/user-1:setPasswordHash"))
        self.assertEqual(payload["hash"]["passwordHashType"], "AD_MD4")
        self.assertEqual(
            payload["hash"]["passwordHash"],
            "8846F7EAEE8FB117AD06BDD830B7586C",
        )
        self.assertFalse(payload["needChange"])

    def test_user_expiration_is_sent_to_the_api(self):
        session = FakeSession()
        creator = UserCreator("token", session=session)
        creator.poll_operation = lambda operation_id, description: {"id": "user-1"}

        creator.create_user(
            userpool_id="pool-1",
            username="user@example.net",
            full_name="Workshop User",
            given_name="Workshop",
            family_name="User",
            email="user@example.net",
            phone_number="",
            password="temporary-password",
            generation_proof="proof",
            expires_at="2099-09-30T21:00:00Z",
        )

        _, payload = session.calls[0]
        self.assertEqual(payload["expiresAt"], "2099-09-30T21:00:00Z")
        self.assertNotIn("phoneNumber", payload)


class UserPoolApiTests(unittest.TestCase):
    def test_create_userpool_uses_expected_settings_and_operation_result(self):
        session = FakeSession()
        creator = UserCreator("token", session=session)
        expected_pool = {
            "id": "pool123",
            "organizationId": "org123",
            "name": "scale-2026-workshop",
            "domains": ["scale-2026-workshop.idp.yandexcloud.net"],
            "status": "ACTIVE",
        }
        creator.poll_operation = lambda operation_id, description: expected_pool

        result = creator.create_userpool(
            "org123",
            "scale-2026-workshop",
            "scale-2026-workshop",
        )

        url, payload = session.calls[0]
        self.assertTrue(url.endswith("/organization-manager/v1/idp/userpools"))
        self.assertEqual(payload["organizationId"], "org123")
        self.assertEqual(payload["defaultSubdomain"], "scale-2026-workshop")
        self.assertTrue(payload["userSettings"]["allowEditSelfPassword"])
        self.assertFalse(payload["userSettings"]["allowEditSelfLogin"])
        self.assertEqual(result, expected_pool)


class YdbProvisioningTests(unittest.TestCase):
    @staticmethod
    def make_args(dry_run=False):
        return SimpleNamespace(
            cloud_id="cloud123",
            workshop_prefix="scale-2026",
            skip_folder_ids=None,
            create_ydb_in_folders=None,
            dry_run=dry_run,
            serverless_storage_size_limit_gb=5,
            serverless_enable_throttling=True,
            serverless_throttling_rcu_limit=10,
            serverless_provisioned_rcu_limit=0,
        )

    def test_one_storage_group_is_recognized_as_dedicated(self):
        database = {
            "storageConfig": {"storageOptions": [{"storageTypeId": "ssd", "groupCount": "1"}]}
        }
        self.assertTrue(has_dedicated_ydb_storage(database))

    def test_explicit_database_types_are_recognized(self):
        self.assertTrue(has_dedicated_ydb_storage({"dedicatedDatabase": {}}))
        self.assertTrue(is_serverless_ydb({"serverlessDatabase": {}}))
        self.assertFalse(is_serverless_ydb({"dedicatedDatabase": {}}))

    def test_dedicated_dry_run_does_not_create_vpc_or_database(self):
        creator = FakeYdbCreator()

        run_ydb_mode(self.make_args(dry_run=True), creator)

        self.assertEqual(creator.vpc_creations, [])
        self.assertEqual(creator.dedicated_creations, [])

    def test_serverless_mode_does_not_create_vpc(self):
        creator = FakeYdbCreator()

        run_serverless_ydb_mode(self.make_args(), creator)

        self.assertEqual(creator.vpc_creations, [])
        self.assertEqual(len(creator.serverless_creations), 1)
        request = creator.serverless_creations[0]
        self.assertEqual(request["folder_id"], "folder123")
        self.assertEqual(request["database_name"], "scale-2026-db-001")
        self.assertEqual(request["storage_size_limit_gb"], 5)
        self.assertTrue(request["enable_throttling_rcu_limit"])

    def test_serverless_dry_run_does_not_create_database(self):
        creator = FakeYdbCreator()

        run_serverless_ydb_mode(self.make_args(dry_run=True), creator)

        self.assertEqual(creator.serverless_creations, [])

    def test_serverless_dry_run_fails_when_preflight_cannot_read_ydb(self):
        with self.assertRaises(UserCreationError):
            run_serverless_ydb_mode(
                self.make_args(dry_run=True),
                FailingYdbListCreator(),
            )

    def test_serverless_payload_uses_bytes_and_has_no_network(self):
        payload = UserCreator._build_serverless_ydb_create_payload(
            folder_id="folder123",
            database_name="ydb-participant-001",
            description="Workshop database",
            storage_size_limit_gb=5,
            enable_throttling_rcu_limit=True,
            throttling_rcu_limit=10,
            provisioned_rcu_limit=0,
        )

        self.assertEqual(payload["serverlessDatabase"]["storageSizeLimit"], str(5 * 1024**3))
        self.assertEqual(payload["serverlessDatabase"]["throttlingRcuLimit"], "10")
        self.assertEqual(payload["serverlessDatabase"]["provisionedRcuLimit"], "0")
        self.assertNotIn("networkId", payload)
        self.assertNotIn("dedicatedDatabase", payload)

    def test_failed_background_create_operation_fails_mode(self):
        with self.assertRaises(UserCreationError):
            run_serverless_ydb_mode(self.make_args(), FailedOperationCreator())

    def test_operation_poller_reports_cloud_operation_failure(self):
        pending = [
            {
                "operation_id": "operation-1",
                "folder_name": "participant-001",
            }
        ]

        result = OperationPoller(FailedOperationCreator()).poll_pending_operations(
            pending,
            "create",
        )

        self.assertEqual(result.successes, 0)
        self.assertEqual(result.failures, 1)
        self.assertEqual(pending, [])

    def test_delete_requires_explicit_folder_ids(self):
        args = self.make_args(dry_run=True)
        args.folder_ids = None
        args.confirm_delete_ydb = False

        with self.assertRaises(ValidationError):
            run_delete_ydb_mode(args, FakeYdbCreator())

    def test_live_delete_requires_confirmation(self):
        args = self.make_args(dry_run=False)
        args.folder_ids = "folder123"
        args.confirm_delete_ydb = False

        with self.assertRaises(ValidationError):
            run_delete_ydb_mode(args, FakeYdbCreator())

    def test_delete_dry_run_does_not_start_deletion(self):
        args = self.make_args(dry_run=True)
        args.folder_ids = "folder123"
        args.confirm_delete_ydb = False
        creator = FakeYdbCreator(
            databases=[
                {
                    "id": "database-1",
                    "name": "ydb-participant-001",
                    "serverlessDatabase": {},
                }
            ]
        )

        run_delete_ydb_mode(args, creator)

        self.assertEqual(creator.database_deletions, [])

    def test_generate_load_uses_distinct_select_log(self):
        creator = FakeYdbCreator(
            databases=[
                {
                    "id": "database-1",
                    "name": "ydb-participant-001",
                    "endpoint": "grpcs://example.net:2135",
                    "storageConfig": {"storageOptions": [{"groupCount": "1"}]},
                }
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            args = SimpleNamespace(
                cloud_id="cloud123",
                folder_ids="folder123",
                skip_folder_ids=None,
                batch_size=1,
                output_dir=temp_dir,
            )

            run_generate_load_mode(args, creator)

            script_path = os.path.join(
                temp_dir,
                "run-mixed-and-select-1.bash",
            )
            with open(script_path, encoding="utf-8") as handle:
                script = handle.read()
            self.assertIn("> mixed-database-1", script)
            self.assertIn("> select-database-1", script)


class PasswordResetTests(unittest.TestCase):
    def test_partial_password_reset_fails_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = os.path.join(temp_dir, "reset.csv")
            args = SimpleNamespace(
                userpool_id="pool123",
                created_users_file=output_file,
                overwrite_output=False,
                user_ids="user-1",
                password_length=11,
            )
            creator = FailingPasswordResetCreator(
                users=[{"id": "user-1", "username": "user@example.net"}]
            )

            with self.assertRaises(UserCreationError):
                run_reset_password_mode(args, creator)


class EnvironmentTests(unittest.TestCase):
    def test_conflicting_prefix_environment_variables_are_rejected(self):
        with patch.dict(
            os.environ,
            {"YC_WORKSHOP_PREFIX": "new", "YC_USER_PREFIX": "old"},
            clear=True,
        ):
            with self.assertRaises(ConfigurationError):
                create_argument_parser()

    def test_cli_can_override_env_dry_run_in_both_directions(self):
        with patch.dict(os.environ, {"YC_DRY_RUN": "true"}, clear=False):
            args = create_argument_parser().parse_args(
                [
                    "--do",
                    "users",
                    "--no-dry-run",
                ]
            )
            self.assertFalse(args.dry_run)

        with patch.dict(os.environ, {"YC_DRY_RUN": "false"}, clear=False):
            args = create_argument_parser().parse_args(
                [
                    "--do",
                    "users",
                    "--dry-run",
                ]
            )
            self.assertTrue(args.dry_run)

    def test_environment_config_and_typed_values(self):
        values = {
            "IAM_TOKEN": "token",
            "YC_REQUEST_TIMEOUT_SECONDS": "12.5",
            "BOOL_VALUE": "yes",
            "INT_VALUE": "17",
        }
        with patch.dict(os.environ, values, clear=True):
            config = EnvironmentConfig.from_env()
            self.assertEqual(config.iam_token, "token")
            self.assertEqual(config.request_timeout_seconds, 12.5)
            self.assertTrue(env_bool("BOOL_VALUE"))
            self.assertEqual(env_int("INT_VALUE"), 17)

    def test_service_account_key_can_be_the_only_credential(self):
        values = {
            "YC_SERVICE_ACCOUNT_KEY_FILE": "authorized_key.json",
        }
        with patch.dict(os.environ, values, clear=True):
            config = EnvironmentConfig.from_env()
            self.assertIsNone(config.iam_token)
            self.assertEqual(
                config.service_account_key_file,
                "authorized_key.json",
            )


class ServiceAccountAuthenticationTests(unittest.TestCase):
    def write_key(self, directory, **overrides):
        key_data = {
            "id": "key-id",
            "service_account_id": "service-account-id",
            "key_algorithm": "RSA_2048",
            "private_key": "private-key-placeholder",
        }
        key_data.update(overrides)
        key_file = os.path.join(directory, "authorized_key.json")
        with open(key_file, "w", encoding="utf-8") as handle:
            json.dump(key_data, handle)
        return key_file

    def test_signed_jwt_is_exchanged_for_iam_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            key_file = self.write_key(temp_dir)
            session = FakeTokenSession()
            with patch("auth.jwt.encode", return_value="signed-jwt") as encode:
                token = create_service_account_iam_token(
                    key_file,
                    request_timeout=15,
                    session=session,
                    now=1000,
                )

            self.assertEqual(token, "service-account-token")
            claims = encode.call_args.args[0]
            self.assertEqual(claims["iss"], "service-account-id")
            self.assertEqual(claims["aud"], IAM_TOKEN_AUDIENCE)
            self.assertEqual(claims["iat"], 1000)
            self.assertEqual(claims["exp"], 4600)
            self.assertEqual(encode.call_args.kwargs["headers"], {"kid": "key-id"})
            _, body, timeout = session.calls[0]
            self.assertEqual(body, {"jwt": "signed-jwt"})
            self.assertEqual(timeout, 15)

    def test_incomplete_key_is_rejected_before_network_call(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            key_file = self.write_key(temp_dir, private_key="")
            with self.assertRaises(ConfigurationError):
                create_service_account_iam_token(
                    key_file,
                    request_timeout=15,
                    session=FakeTokenSession(),
                )


if __name__ == "__main__":
    unittest.main()
