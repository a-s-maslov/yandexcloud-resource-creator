#!/usr/bin/env python3
"""
UserCreator class for Yandex Cloud User Creation CLI Tool

This module handles user creation, folder creation, and access management
using the Yandex Cloud organization-manager and resource-manager APIs.
"""

import logging
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests
from Crypto.Hash import MD4

from config import Constants
from exceptions import UserCreationError

logger = logging.getLogger(__name__)


class TimeoutSession(requests.Session):
    """Requests session that applies a finite timeout to every HTTP call."""

    def __init__(self, timeout: float):
        super().__init__()
        self.default_timeout = timeout

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self.default_timeout)
        return super().request(method, url, **kwargs)


class UserCreator:
    """Handles user creation, folder creation, and access management in Yandex Cloud"""

    def __init__(self, iam_token: str, request_timeout: float = 30.0, session=None):
        self.iam_token = iam_token
        self.session = session or TimeoutSession(request_timeout)
        self.session.headers.update(
            {"Authorization": f"Bearer {iam_token}", "Content-Type": "application/json"}
        )

    def get_cloud(self, cloud_id: str) -> dict:
        """Return a cloud, including the organization it belongs to."""
        url = f"https://resource-manager.api.cloud.yandex.net/resource-manager/v1/clouds/{cloud_id}"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    f"Get cloud failed: {data['error'].get('message', data['error'])}"
                )
            return data
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to get cloud %s: %s %s",
                cloud_id,
                e,
                getattr(e.response, "text", ""),
            )
            raise UserCreationError(f"Get cloud failed: {e}")

    def list_organization_access_policies(self, organization_id: str) -> list:
        """Return access policy bindings assigned to an organization."""
        url = (
            "https://organization-manager.api.cloud.yandex.net/"
            f"organization-manager/v1/organizations/{organization_id}:"
            "listAccessPolicyBindings"
        )
        bindings = []
        page_token = None
        while True:
            params = {"pageSize": "1000"}
            if page_token:
                params["pageToken"] = page_token
            try:
                response = self.session.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    raise UserCreationError(
                        "List organization access policies failed: "
                        f"{data['error'].get('message', data['error'])}"
                    )
                bindings.extend(data.get("accessPolicyBindings", []))
                page_token = data.get("nextPageToken")
                if not page_token:
                    return bindings
            except requests.exceptions.RequestException as e:
                raise UserCreationError(f"List organization access policies failed: {e}")

    def bind_organization_access_policy(
        self,
        organization_id: str,
        template_id: str,
    ) -> None:
        """Bind a parameterless access policy template to an organization."""
        url = (
            "https://organization-manager.api.cloud.yandex.net/"
            f"organization-manager/v1/organizations/{organization_id}:"
            "bindAccessPolicy"
        )
        payload = {
            "accessPolicyBinding": {
                "accessPolicyTemplateId": template_id,
                "parameters": {},
            }
        }
        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    "Bind organization access policy failed: "
                    f"{data['error'].get('message', data['error'])}"
                )
            self.poll_operation(
                data["id"],
                f"bind access policy {template_id} to organization {organization_id}",
            )
        except requests.exceptions.RequestException as e:
            raise UserCreationError(f"Bind organization access policy failed: {e}")

    def get_userpool(self, userpool_id: str) -> dict:
        """Return one Identity Hub User Pool."""
        url = (
            "https://organization-manager.api.cloud.yandex.net/"
            f"organization-manager/v1/idp/userpools/{userpool_id}"
        )
        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    f"Get User Pool failed: {data['error'].get('message', data['error'])}"
                )
            return data
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to get User Pool %s: %s %s",
                userpool_id,
                e,
                getattr(e.response, "text", ""),
            )
            raise UserCreationError(f"Get User Pool failed: {e}")

    def list_userpools(self, organization_id: str, page_size: int = 1000) -> list:
        """List every Identity Hub User Pool in an organization."""
        url = (
            "https://organization-manager.api.cloud.yandex.net/"
            "organization-manager/v1/idp/userpools"
        )
        userpools = []
        page_token = None
        while True:
            params = {
                "organizationId": organization_id,
                "pageSize": str(page_size),
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                response = self.session.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    raise UserCreationError(
                        f"List User Pools failed: {data['error'].get('message', data['error'])}"
                    )
                userpools.extend(data.get("userpools", []))
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
            except requests.exceptions.RequestException as e:
                logger.error(
                    "Failed to list User Pools in organization %s: %s %s",
                    organization_id,
                    e,
                    getattr(e.response, "text", ""),
                )
                raise UserCreationError(f"List User Pools failed: {e}")
        return userpools

    def create_userpool(
        self,
        organization_id: str,
        name: str,
        default_subdomain: str,
    ) -> dict:
        """Create a User Pool and return the completed resource."""
        url = (
            "https://organization-manager.api.cloud.yandex.net/"
            "organization-manager/v1/idp/userpools"
        )
        payload = {
            "organizationId": organization_id,
            "name": name,
            "description": f"Local accounts in {name}",
            "defaultSubdomain": default_subdomain,
            "userSettings": {
                "allowEditSelfPassword": True,
                "allowEditSelfInfo": False,
                "allowEditSelfContacts": False,
                "allowEditSelfLogin": False,
            },
        }
        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    f"User Pool creation failed: {data['error'].get('message', data['error'])}"
                )
            operation_id = data["id"]
            return self.poll_operation(
                operation_id,
                f"User Pool creation for {name}",
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to create User Pool %s: %s %s",
                name,
                e,
                getattr(e.response, "text", ""),
            )
            raise UserCreationError(f"User Pool creation failed: {e}")

    def generate_password(
        self,
        length: int = Constants.DEFAULT_PASSWORD_LENGTH,
    ) -> Tuple[str, Optional[str]]:
        """Generate a copy-friendly password with three character classes."""
        if length < 3:
            raise ValueError("Password length must be at least 3")
        lowers = "abcdefghijkmnopqrstuvwxyz"
        uppers = "ABCDEFGHJKLMNPQRSTUVWXYZ"
        digits = "23456789"
        alphabet = lowers + uppers + digits
        characters = [
            secrets.choice(lowers),
            secrets.choice(uppers),
            secrets.choice(digits),
        ]
        characters.extend(secrets.choice(alphabet) for _ in range(length - 3))
        secrets.SystemRandom().shuffle(characters)
        return "".join(characters), None

    # sample response
    #     {
    #   "id": "string",
    #   "description": "string",
    #   "createdAt": "string",
    #   "createdBy": "string",
    #   "modifiedAt": "string",
    #   "done": "boolean",
    #   "metadata": {
    #     "userId": "string"
    #   },
    #   // Includes only one of the fields `error`, `response`
    #   "error": {
    #     "code": "integer",
    #     "message": "string",
    #     "details": [
    #       "object"
    #     ]
    #   },
    #   "response": {
    #     "id": "string",
    #     "userpoolId": "string",
    #     "status": "string",
    #     "username": "string",
    #     "fullName": "string",
    #     "givenName": "string",
    #     "familyName": "string",
    #     "email": "string",
    #     "phoneNumber": "string",
    #     "createdAt": "string",
    #     "updatedAt": "string",
    #     "externalId": "string"
    #   }
    #   // end of the list of possible fields
    # }

    def create_user(
        self,
        userpool_id: str,
        username: str,
        full_name: str,
        given_name: str,
        family_name: str,
        email: str,
        phone_number: str,
        password: str,
        generation_proof: str,
        expires_at: Optional[str] = None,
    ) -> str:
        """Create a user using Yandex Cloud API"""
        url = "https://organization-manager.api.cloud.yandex.net/organization-manager/v1/idp/users"

        payload = {
            "userpoolId": userpool_id,
            "username": username,
            "fullName": full_name,
            "givenName": given_name,
            "familyName": family_name,
            "email": email,
            "passwordSpec": {"password": password},
            "isActive": True,
        }
        if generation_proof:
            payload["passwordSpec"]["generationProof"] = generation_proof
        if phone_number:
            payload["phoneNumber"] = phone_number
        if expires_at:
            payload["expiresAt"] = expires_at

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logger.error(f"Failed to create user {username}: {data['error']}")
                raise UserCreationError(f"User creation failed: {data['error']['message']}")

            # Get operation ID and poll until completion
            operation_id = data["id"]
            operation_description = f"user creation for {username}"

            # Poll the operation until it's complete
            operation_response = self.poll_operation(operation_id, operation_description)
            # Extract user_id from the completed operation response
            user_id = operation_response["id"]

            logger.info(f"User creation request submitted for {username}")
            return user_id

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create user {username}: {e} {getattr(e.response, 'text', '')}")
            raise UserCreationError(f"User creation failed: {e}")

    def create_folder(self, cloud_id: str, folder_name: str, description: str = None) -> str:
        """Create a folder in Yandex Cloud"""
        url = "https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders"

        payload = {
            "cloudId": cloud_id,
            "name": folder_name,
            "description": description or f"Personal folder for user {folder_name}",
            "labels": {},
        }

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                logger.error(f"Failed to create folder {folder_name}: {data['error']}")
                raise UserCreationError(f"Folder creation failed: {data['error']['message']}")

            # Get operation ID and poll until completion
            operation_id = data["id"]
            operation_description = f"folder creation for {folder_name}"
            # Poll the operation until it's complete
            operation_response = self.poll_operation(operation_id, operation_description)
            folder_id = operation_response["id"]
            logger.info(f"Folder created successfully: {folder_name} (ID: {folder_id})")
            return folder_id

        except requests.exceptions.RequestException as e:
            response_text = getattr(e.response, "text", "")
            logger.error(f"Failed to create folder {folder_name}: {e} {response_text}")
            details = f" {response_text}" if response_text else ""
            raise UserCreationError(f"Folder creation failed: {e}{details}")

    def start_folder_deletion(
        self,
        folder_id: str,
        delete_after: str,
    ) -> str:
        """Start deletion of one explicitly selected folder."""
        url = (
            f"https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders/{folder_id}"
        )
        try:
            response = self.session.delete(
                url,
                params={"deleteAfter": delete_after},
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    f"Folder deletion failed: {data['error'].get('message', data['error'])}"
                )
            operation_id = data["id"]
            logger.info(
                "Folder deletion started: %s (op: %s)",
                folder_id,
                operation_id,
            )
            return operation_id
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to start folder deletion for %s: %s %s",
                folder_id,
                e,
                getattr(e.response, "text", ""),
            )
            raise UserCreationError(f"Folder deletion failed: {e}")

    def delete_user(self, user_id: str) -> None:
        """Delete a local User Pool account and wait for completion."""
        url = (
            "https://organization-manager.api.cloud.yandex.net/"
            f"organization-manager/v1/idp/users/{user_id}"
        )
        try:
            response = self.session.delete(url)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    f"User deletion failed: {data['error'].get('message', data['error'])}"
                )
            self.poll_operation(data["id"], f"user deletion for {user_id}")
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to delete user %s: %s %s",
                user_id,
                e,
                getattr(e.response, "text", ""),
            )
            raise UserCreationError(f"User deletion failed: {e}")

    @staticmethod
    def _ad_md4_password_hash(password: str) -> str:
        """Return the Active Directory-compatible NT hash for a password."""
        if not password:
            raise ValueError("Password must not be empty")
        return MD4.new(password.encode("utf-16-le")).hexdigest().upper()

    def set_user_password_hash(
        self,
        user_id: str,
        password: str,
        need_change: bool = False,
    ) -> None:
        """Import a password hash and control first-login password rotation."""
        url = (
            "https://organization-manager.api.cloud.yandex.net/"
            f"organization-manager/v1/idp/users/{user_id}:setPasswordHash"
        )
        payload = {
            "hash": {
                "passwordHash": self._ad_md4_password_hash(password),
                "passwordHashType": "AD_MD4",
            },
            "needChange": need_change,
        }
        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    f"Set password hash failed: {data['error'].get('message', data['error'])}"
                )
            self.poll_operation(
                data["id"],
                f"permanent password setup for user {user_id}",
            )
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to set password hash for user %s: %s %s",
                user_id,
                e,
                getattr(e.response, "text", ""),
            )
            raise UserCreationError(f"Set password hash failed: {e}")

    def delete_folder(
        self,
        folder_id: str,
        immediate: bool = False,
        wait: bool = False,
    ) -> str:
        """Start deletion of a folder and optionally wait for completion."""
        url = (
            f"https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders/{folder_id}"
        )
        params = None
        if immediate:
            params = {
                "deleteAfter": datetime.now(timezone.utc)
                .isoformat()
                .replace(
                    "+00:00",
                    "Z",
                )
            }
        try:
            response = self.session.delete(url, params=params)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    f"Folder deletion failed: {data['error'].get('message', data['error'])}"
                )
            operation_id = data["id"]
            if wait:
                self.poll_operation(operation_id, f"folder deletion for {folder_id}")
            return operation_id
        except requests.exceptions.RequestException as e:
            logger.error(
                "Failed to delete folder %s: %s %s",
                folder_id,
                e,
                getattr(e.response, "text", ""),
            )
            raise UserCreationError(f"Folder deletion failed: {e}")

    def get_folder(self, folder_id: str) -> dict:
        """Return folder metadata used to derive human-readable resource names."""
        url = (
            f"https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders/{folder_id}"
        )
        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    f"Get folder failed: {data['error'].get('message', data['error'])}"
                )
            return data
        except requests.exceptions.RequestException as e:
            raise UserCreationError(f"Get folder failed: {e}")

    def update_folder_access(
        self,
        folder_id: str,
        user_id: str,
        role_id: str,
        action: str,
    ) -> None:
        """Add or remove one user access binding on a folder."""
        if action not in {"ADD", "REMOVE"}:
            raise ValueError("Folder access action must be ADD or REMOVE")
        url = f"https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders/{folder_id}:updateAccessBindings"

        payload = {
            "accessBindingDeltas": [
                {
                    "action": action,
                    "accessBinding": {
                        "roleId": role_id,
                        "subject": {"id": user_id, "type": "userAccount"},
                    },
                }
            ]
        }

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logger.error(
                    f"Failed to update access for user {user_id} on folder {folder_id}: {data['error']}"
                )
                raise UserCreationError(f"Access update failed: {data['error']['message']}")

            operation_id = data["id"]
            self.poll_operation(
                operation_id,
                f"folder access {action.lower()} for user {user_id} on folder {folder_id}",
            )
            logger.info(
                "Folder access updated: %s user %s -> role %s -> folder %s",
                action,
                user_id,
                role_id,
                folder_id,
            )

        except requests.exceptions.RequestException as e:
            logger.error(
                f"Failed to update access for user {user_id} on folder {folder_id}: {e} {getattr(e.response, 'text', '')}"
            )
            raise UserCreationError(f"Access update failed: {e}")

    def grant_folder_access(
        self,
        folder_id: str,
        user_id: str,
        role_id: str = Constants.DEFAULT_FOLDER_ROLE,
    ) -> None:
        """Grant access to a folder for a user."""
        self.update_folder_access(folder_id, user_id, role_id, "ADD")

    def revoke_folder_access(
        self,
        folder_id: str,
        user_id: str,
        role_id: str,
    ) -> None:
        """Revoke one folder role from a user."""
        self.update_folder_access(folder_id, user_id, role_id, "REMOVE")

    def list_folder_access_bindings(self, folder_id: str) -> list:
        """Return access bindings directly assigned to a folder."""
        url = f"https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders/{folder_id}:listAccessBindings"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    f"List folder access failed: {data['error'].get('message', data['error'])}"
                )
            return data.get("accessBindings", [])
        except requests.exceptions.RequestException as e:
            raise UserCreationError(f"List folder access failed: {e}")

    def grant_cloud_access(self, cloud_id: str, user_id: str, role_id: str = "editor") -> None:
        """Grant access to a cloud for a user"""
        url = f"https://resource-manager.api.cloud.yandex.net/resource-manager/v1/clouds/{cloud_id}:updateAccessBindings"

        payload = {
            "accessBindingDeltas": [
                {
                    "action": "ADD",
                    "accessBinding": {
                        "roleId": role_id,
                        "subject": {"id": user_id, "type": "userAccount"},
                    },
                }
            ]
        }

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logger.error(
                    f"Failed to grant cloud access for user {user_id} to cloud {cloud_id}: {data['error']}"
                )
                raise UserCreationError(f"Cloud access grant failed: {data['error']['message']}")

            # Get operation ID and poll until completion
            operation_id = data["id"]
            operation_description = f"cloud access grant for user {user_id} to cloud {cloud_id}"

            # Poll the operation until it's complete
            self.poll_operation(operation_id, operation_description)

            logger.info(
                f"Cloud access granted successfully: user {user_id} -> role {role_id} -> cloud {cloud_id}"
            )

        except requests.exceptions.RequestException as e:
            logger.error(
                f"Failed to grant cloud access for user {user_id} to cloud {cloud_id}: {e} {getattr(e.response, 'text', '')}"
            )
            raise UserCreationError(f"Cloud access grant failed: {e}")

    def create_vpc_with_subnets(
        self, folder_id: str, network_name: str = None, description: str = None
    ) -> tuple:
        """Create a VPC network with 3 subnets in different zones"""
        if not network_name:
            network_name = f"vpc-network-{folder_id}"
        if not description:
            description = f"VPC network for folder {folder_id}"

        # Create the network first
        network_id = self._create_network(folder_id, network_name, description)

        # Create 3 subnets in different zones
        zones = Constants.YDB_AVAILABILITY_ZONES
        subnet_ids = []

        for i, zone in enumerate(zones, 1):
            subnet_name = f"{network_name}-subnet-{zone}"
            subnet_description = f"Subnet in {zone} for {network_name}"
            cidr_block = Constants.VPC_CIDR_BLOCKS[i - 1]

            subnet_id = self._create_subnet(
                folder_id=folder_id,
                network_id=network_id,
                zone_id=zone,
                name=subnet_name,
                description=subnet_description,
                cidr_block=cidr_block,
            )
            subnet_ids.append(subnet_id)

        logger.info(
            f"VPC network created successfully: {network_name} (ID: {network_id}) with subnets: {subnet_ids}"
        )
        return network_id, subnet_ids

    def _create_network(self, folder_id: str, name: str, description: str) -> str:
        """Create a VPC network"""
        url = "https://vpc.api.cloud.yandex.net/vpc/v1/networks"

        payload = {"folderId": folder_id, "name": name, "description": description, "labels": {}}

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logger.error(f"Failed to create network {name}: {data['error']}")
                raise UserCreationError(f"Network creation failed: {data['error']['message']}")

            # Get operation ID and poll until completion
            operation_id = data["id"]
            operation_description = f"network creation for {name}"

            # Poll the operation until it's complete
            operation_response = self.poll_operation(operation_id, operation_description)

            network_id = operation_response["id"]
            logger.info(f"Network created successfully: {name} (ID: {network_id})")
            return network_id

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create network {name}: {e} {getattr(e.response, 'text', '')}")
            raise UserCreationError(f"Network creation failed: {e}")

    def _create_subnet(
        self,
        folder_id: str,
        network_id: str,
        zone_id: str,
        name: str,
        description: str,
        cidr_block: str,
    ) -> str:
        """Create a subnet in a specific zone"""
        url = "https://vpc.api.cloud.yandex.net/vpc/v1/subnets"

        payload = {
            "folderId": folder_id,
            "name": name,
            "description": description,
            "labels": {},
            "networkId": network_id,
            "zoneId": zone_id,
            "v4CidrBlocks": [cidr_block],
        }

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logger.error(f"Failed to create subnet {name}: {data['error']}")
                raise UserCreationError(f"Subnet creation failed: {data['error']['message']}")

            # Get operation ID and poll until completion
            operation_id = data["id"]
            operation_description = f"subnet creation for {name} in {zone_id}"

            # Poll the operation until it's complete
            operation_response = self.poll_operation(operation_id, operation_description)

            subnet_id = operation_response["id"]
            logger.info(
                f"Subnet created successfully: {name} (ID: {subnet_id}) in zone {zone_id} with CIDR {cidr_block}"
            )
            return subnet_id

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create subnet {name}: {e} {getattr(e.response, 'text', '')}")
            raise UserCreationError(f"Subnet creation failed: {e}")

    def create_ydb_database(
        self,
        folder_id: str,
        network_id: str,
        subnet_ids: list,
        database_name: str = None,
        description: str = None,
    ) -> str:
        """Create a YDB database with the specified configuration"""
        if not database_name:
            database_name = f"ydb-database-{folder_id}"
        if not description:
            description = f"YDB database for folder {folder_id}"

        # Validate resource name pattern: /|[a-zA-Z]([-_a-zA-Z0-9]{0,61}[a-zA-Z0-9])?/
        if not self._is_valid_ydb_resource_name(database_name):
            raise UserCreationError(
                f"Invalid database name '{database_name}'. Must match pattern /|[a-zA-Z]([-_a-zA-Z0-9]{{0,61}}[a-zA-Z0-9])?/"
            )

        # Build payload and start operation via shared helper, then poll to completion
        payload = self._build_ydb_create_payload(
            folder_id=folder_id,
            network_id=network_id,
            subnet_ids=subnet_ids,
            database_name=database_name,
            description=description,
        )
        operation_id = self._start_ydb_database_with_payload(payload, database_name)
        operation_description = f"YDB database creation for {database_name}"
        operation_response = self.poll_operation(operation_id, operation_description)
        database_id = operation_response["id"]
        logger.info(f"YDB database created successfully: {database_name} (ID: {database_id})")
        return database_id

    def start_ydb_database(
        self,
        folder_id: str,
        network_id: str,
        subnet_ids: list,
        database_name: str = None,
        description: str = None,
    ) -> str:
        """Start YDB database creation and return operation ID without waiting for completion"""
        if not database_name:
            database_name = f"ydb-database-{folder_id}"
        if not description:
            description = f"YDB database for folder {folder_id}"

        if not self._is_valid_ydb_resource_name(database_name):
            raise UserCreationError(
                f"Invalid database name '{database_name}'. Must match pattern /|[a-zA-Z]([-_a-zA-Z0-9]{0, 61}[a-zA-Z0-9])?/"
            )

        payload = self._build_ydb_create_payload(
            folder_id=folder_id,
            network_id=network_id,
            subnet_ids=subnet_ids,
            database_name=database_name,
            description=description,
        )
        return self._start_ydb_database_with_payload(payload, database_name)

    def start_serverless_ydb_database(
        self,
        folder_id: str,
        database_name: str,
        description: str,
        storage_size_limit_gb: int,
        enable_throttling_rcu_limit: bool,
        throttling_rcu_limit: int,
        provisioned_rcu_limit: int,
    ) -> str:
        """Start Serverless YDB creation without provisioning a VPC."""
        if not self._is_valid_ydb_resource_name(database_name):
            raise UserCreationError(
                f"Invalid database name '{database_name}'. Must match YDB naming rules"
            )
        payload = self._build_serverless_ydb_create_payload(
            folder_id=folder_id,
            database_name=database_name,
            description=description,
            storage_size_limit_gb=storage_size_limit_gb,
            enable_throttling_rcu_limit=enable_throttling_rcu_limit,
            throttling_rcu_limit=throttling_rcu_limit,
            provisioned_rcu_limit=provisioned_rcu_limit,
        )
        return self._start_ydb_database_with_payload(payload, database_name)

    def _build_ydb_create_payload(
        self,
        folder_id: str,
        network_id: str,
        subnet_ids: list,
        database_name: str,
        description: str,
    ) -> dict:
        """Build YDB create payload (shared by start and create)."""
        return {
            "folderId": folder_id,
            "name": database_name,
            "description": description,
            "resourcePresetId": Constants.YDB_RESOURCE_PRESET,
            "storageConfig": {
                "storageOptions": [
                    {
                        "storageTypeId": Constants.YDB_STORAGE_TYPE,
                        "groupCount": Constants.YDB_GROUP_COUNT,
                    }
                ]
            },
            "scalePolicy": {"fixedScale": {"size": Constants.YDB_SCALE_SIZE}},
            "networkId": network_id,
            "subnetIds": subnet_ids,
            "dedicatedDatabase": {
                "resourcePresetId": Constants.YDB_RESOURCE_PRESET,
                "storageConfig": {
                    "storageOptions": [
                        {
                            "storageTypeId": Constants.YDB_STORAGE_TYPE,
                            "groupCount": Constants.YDB_GROUP_COUNT,
                        }
                    ]
                },
                "scalePolicy": {"fixedScale": {"size": Constants.YDB_SCALE_SIZE}},
                "networkId": network_id,
                "subnetIds": subnet_ids,
                "assignPublicIps": False,
            },
            "assignPublicIps": False,
            "labels": {},
        }

    @staticmethod
    def _build_serverless_ydb_create_payload(
        folder_id: str,
        database_name: str,
        description: str,
        storage_size_limit_gb: int,
        enable_throttling_rcu_limit: bool,
        throttling_rcu_limit: int,
        provisioned_rcu_limit: int,
    ) -> dict:
        """Build the REST payload for a Serverless YDB database."""
        gibibyte = 1024**3
        return {
            "folderId": folder_id,
            "name": database_name,
            "description": description,
            "serverlessDatabase": {
                "storageSizeLimit": str(storage_size_limit_gb * gibibyte),
                "enableThrottlingRcuLimit": enable_throttling_rcu_limit,
                "throttlingRcuLimit": str(throttling_rcu_limit),
                "provisionedRcuLimit": str(provisioned_rcu_limit),
            },
            "labels": {},
        }

    def _start_ydb_database_with_payload(self, payload: dict, database_name: str) -> str:
        """POST YDB create with given payload and return operation id."""
        url = "https://ydb.api.cloud.yandex.net/ydb/v1/databases"
        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                logger.error(f"Failed to start YDB database {database_name}: {data['error']}")
                raise UserCreationError(f"YDB database start failed: {data['error']['message']}")
            operation_id = data["id"]
            logger.info(f"YDB create operation started for {database_name} (op: {operation_id})")
            return operation_id
        except requests.exceptions.RequestException as e:
            logger.error(
                f"Failed to start YDB database {database_name}: {e} {getattr(e.response, 'text', '')}"
            )
            raise UserCreationError(f"YDB database start failed: {e}")

    def start_ydb_database_deletion(self, database_id: str) -> str:
        """Start YDB database deletion and return operation ID without waiting for completion"""
        url = f"https://ydb.api.cloud.yandex.net/ydb/v1/databases/{database_id}"

        try:
            response = self.session.delete(url)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logger.error(
                    f"Failed to start YDB database deletion {database_id}: {data['error']}"
                )
                raise UserCreationError(
                    f"YDB database deletion start failed: {data['error']['message']}"
                )

            operation_id = data["id"]
            logger.info(
                f"YDB delete operation started for database {database_id} (op: {operation_id})"
            )
            return operation_id

        except requests.exceptions.RequestException as e:
            logger.error(
                f"Failed to start YDB database deletion {database_id}: {e} {response.text if 'response' in locals() else ''}"
            )
            raise UserCreationError(f"YDB database deletion start failed: {e}")

    def get_operation_status(self, operation_id: str) -> dict:
        """Fetch operation status once (non-blocking) and return the JSON."""
        url = f"https://operation.api.cloud.yandex.net/operations/{operation_id}"
        for attempt in range(1, Constants.MAX_POLL_RETRIES + 1):
            try:
                response = self.session.get(url)
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as e:
                if attempt >= Constants.MAX_POLL_RETRIES:
                    logger.error(
                        f"Failed to fetch operation status for {operation_id} after {attempt} attempts: {e}"
                    )
                    raise UserCreationError(f"Operation status fetch failed: {e}")
                delay = 2 ** (attempt - 1)
                logger.warning(
                    f"Fetch status retry {attempt}/{Constants.MAX_POLL_RETRIES} for op {operation_id} in {delay}s due to error: {e}"
                )
                time.sleep(delay)

    def list_users_in_userpool(self, userpool_id: str, page_size: int = 1000) -> list:
        """List users in a userpool, handling pagination."""
        url = "https://organization-manager.api.cloud.yandex.net/organization-manager/v1/idp/users"
        users = []
        page_token = None
        while True:
            params = {
                "userpoolId": userpool_id,
                "pageSize": str(page_size),
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                response = self.session.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    logger.error(f"Failed to list users in userpool {userpool_id}: {data['error']}")
                    raise UserCreationError(f"List users failed: {data['error']['message']}")
                batch = data.get("users", [])
                users.extend(batch)
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to list users in userpool {userpool_id}: {e}")
                raise UserCreationError(f"List users failed: {e}")
        logger.info(f"Listed {len(users)} users in userpool {userpool_id}")
        return users

    def set_others_password(
        self,
        user_id: str,
        password: str,
        generation_proof: Optional[str] = None,
    ) -> None:
        """Set password for a user as an administrator, with operation polling."""
        url = f"https://organization-manager.api.cloud.yandex.net/organization-manager/v1/idp/users/{user_id}:setOthersPassword"
        password_spec = {"password": password}
        if generation_proof:
            password_spec["generationProof"] = generation_proof
        payload = {"passwordSpec": password_spec}
        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                logger.error(
                    f"Failed to start setOthersPassword for user {user_id}: {data['error']}"
                )
                raise UserCreationError(f"setOthersPassword failed: {data['error']['message']}")
            operation_id = data["id"]
            op_desc = f"setOthersPassword for user {user_id}"
            self.poll_operation(operation_id, op_desc)
            logger.info(f"Password reset completed for user {user_id}")
        except requests.exceptions.RequestException as e:
            logger.error(
                f"Failed to call setOthersPassword for user {user_id}: {e} {response.text if 'response' in locals() else ''}"
            )
            raise UserCreationError(f"setOthersPassword request failed: {e}")

    def _is_valid_ydb_resource_name(self, name: str) -> bool:
        """Validate YDB resource name pattern: /|[a-zA-Z]([-_a-zA-Z0-9]{0,61}[a-zA-Z0-9])?/"""
        # Pattern: start with letter, then 0-61 chars of letters/numbers/dash/underscore, end with letter or number
        pattern = r"^[a-zA-Z]([-_a-zA-Z0-9]{0,61}[a-zA-Z0-9])?$"
        return bool(re.match(pattern, name))

    def list_folders(self, cloud_id: str, page_size: int = 1000) -> list:
        """List all folders in a cloud, handling pagination."""
        url = "https://resource-manager.api.cloud.yandex.net/resource-manager/v1/folders"
        folders = []
        page_token = None
        while True:
            params = {"cloudId": cloud_id, "pageSize": str(page_size)}
            if page_token:
                params["pageToken"] = page_token
            try:
                response = self.session.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    logger.error(f"Failed to list folders in cloud {cloud_id}: {data['error']}")
                    raise UserCreationError(f"Failed to list folders: {data['error']['message']}")
                folders.extend(data.get("folders", []))
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to list folders in cloud {cloud_id}: {e}")
                raise UserCreationError(f"Failed to list folders: {e}")
        logger.info(f"Found {len(folders)} folders in cloud {cloud_id}")
        return folders

    def list_ydb_databases_in_folder(self, folder_id: str, page_size: int = 1000) -> list:
        """List YDB databases in a folder (handles pagination)."""
        url = "https://ydb.api.cloud.yandex.net/ydb/v1/databases"
        databases = []
        page_token = None
        while True:
            params = {
                "folderId": folder_id,
                "pageSize": str(page_size),
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                response = self.session.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    logger.error(
                        f"Failed to list YDB databases in folder {folder_id}: {data['error']}"
                    )
                    raise UserCreationError(
                        f"List YDB databases failed: {data['error']['message']}"
                    )
                databases.extend(data.get("databases", []))
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to list YDB databases in folder {folder_id}: {e}")
                raise UserCreationError(f"List YDB databases failed: {e}")
        logger.info(f"Listed {len(databases)} YDB databases in folder {folder_id}")
        return databases

    def get_ydb_database(self, database_id: str) -> dict:
        """Return one YDB database by ID."""
        url = f"https://ydb.api.cloud.yandex.net/ydb/v1/databases/{database_id}"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise UserCreationError(
                    f"Get YDB database failed: {data['error'].get('message', data['error'])}"
                )
            return data
        except requests.exceptions.RequestException as e:
            raise UserCreationError(f"Get YDB database failed: {e}")

    def list_networks(self, folder_id: str) -> list:
        """List all networks in a folder"""
        url = "https://vpc.api.cloud.yandex.net/vpc/v1/networks"

        params = {"folderId": folder_id}
        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logger.error(f"Failed to list networks in folder {folder_id}: {data['error']}")
                raise UserCreationError(f"Failed to list networks: {data['error']['message']}")

            networks = data.get("networks", [])
            logger.info(f"Found {len(networks)} networks in folder {folder_id}")
            return networks

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to list networks in folder {folder_id}: {e}")
            raise UserCreationError(f"Failed to list networks: {e}")

    def list_subnets(self, network_id: str) -> list:
        """List all subnets in a network"""
        url = f"https://vpc.api.cloud.yandex.net/vpc/v1/networks/{network_id}/subnets"

        try:
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()

            if "error" in data:
                logger.error(f"Failed to list subnets for network {network_id}: {data['error']}")
                raise UserCreationError(f"Failed to list subnets: {data['error']['message']}")

            subnets = data.get("subnets", [])
            logger.info(f"Found {len(subnets)} subnets in network {network_id}")
            return subnets

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to list subnets for network {network_id}: {e}")
            raise UserCreationError(f"Failed to list subnets: {e}")

    def check_existing_vpc(self, folder_id: str) -> tuple:
        """Check if folder has existing VPC with subnets in all required zones"""
        required_zones = Constants.YDB_AVAILABILITY_ZONES

        try:
            # List networks in the folder
            networks = self.list_networks(folder_id)

            if not networks:
                logger.info(f"No networks found in folder {folder_id}")
                return None, []

            # Check each network for complete subnet coverage
            for network in networks:
                network_id = network["id"]
                network_name = network["name"]

                # Get subnets for this network
                subnets = self.list_subnets(network_id)

                if not subnets:
                    logger.info(f"Network {network_name} (ID: {network_id}) has no subnets")
                    continue

                # Check if we have subnets in all required zones
                subnet_zones = set()
                subnet_ids = []

                for subnet in subnets:
                    zone_id = subnet["zoneId"]
                    subnet_zones.add(zone_id)
                    subnet_ids.append(subnet["id"])

                # Check if all required zones are covered
                if subnet_zones.issuperset(set(required_zones)):
                    logger.info(
                        f"Found existing VPC {network_name} (ID: {network_id}) with subnets in all required zones: {subnet_zones}"
                    )
                    return network_id, subnet_ids
                else:
                    missing_zones = set(required_zones) - subnet_zones
                    logger.info(
                        f"Network {network_name} (ID: {network_id}) missing subnets in zones: {missing_zones}"
                    )

            logger.info(f"No complete VPC found in folder {folder_id}")
            return None, []

        except UserCreationError:
            # Re-raise UserCreationError as-is
            raise
        except Exception as e:
            logger.error(f"Error checking existing VPC in folder {folder_id}: {e}")
            raise UserCreationError(f"Failed to check existing VPC: {e}")

    def poll_operation(self, operation_id: str, operation_description: str = "operation") -> dict:
        """Poll operation status until completion"""
        url = f"https://operation.api.cloud.yandex.net/operations/{operation_id}"

        logger.info(f"Starting polling for {operation_description} (ID: {operation_id})")
        start_time = time.time()

        while True:
            try:
                # GET with retries and backoff
                for attempt in range(1, Constants.MAX_POLL_RETRIES + 1):
                    try:
                        response = self.session.get(url)
                        response.raise_for_status()
                        data = response.json()
                        break
                    except requests.exceptions.RequestException as e:
                        if attempt >= Constants.MAX_POLL_RETRIES:
                            raise
                        delay = 2 ** (attempt - 1)
                        logger.warning(
                            f"Poll retry {attempt}/{Constants.MAX_POLL_RETRIES} for {operation_description} (ID: {operation_id}) in {delay}s due to error: {e}"
                        )
                        time.sleep(delay)

                done = data.get("done", False)

                # Check if operation is done
                if done:
                    # Operation finished, check for errors
                    if "error" in data and data["error"]:
                        error = data["error"]
                        elapsed_time = time.time() - start_time
                        logger.error(
                            f"Operation {operation_id} failed after {elapsed_time:.2f}s: "
                            f"status={error.get('code', 'unknown')}, "
                            f"message='{error.get('message', 'no message')}', "
                            f"details={error.get('details', {})}"
                        )
                        raise UserCreationError(
                            f"Operation {operation_description} failed: {error.get('message', 'Unknown error')}"
                        )
                    else:
                        elapsed_time = time.time() - start_time
                        logger.info(
                            f"Operation {operation_description} completed successfully (ID: {operation_id}) in {elapsed_time:.2f}s"
                        )
                        return data.get("response", {})
                else:
                    # Operation not done yet, check for errors
                    if "error" in data and data["error"]:
                        error = data["error"]
                        elapsed_time = time.time() - start_time
                        logger.warning(
                            f"Operation {operation_id} has failures during execution after {elapsed_time:.2f}s: "
                            f"status={error.get('code', 'unknown')}, "
                            f"message='{error.get('message', 'no message')}', "
                            f"details={error.get('details', {})}"
                        )
                        raise UserCreationError(
                            f"Operation {operation_description} failed during execution: {error.get('message', 'Unknown error')}"
                        )

                    # Operation still in progress, wait and continue polling
                    logger.debug(
                        f"Operation {operation_description} still in progress (ID: {operation_id})"
                    )
                    time.sleep(2)

            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to poll operation {operation_id}: {e}")
                raise UserCreationError(f"Failed to poll {operation_description}: {e}")
