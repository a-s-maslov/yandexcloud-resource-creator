# Yandex Cloud Resource Creator

A Python CLI for preparing workshop users and selected Yandex Cloud resources.
Configuration is loaded from `.env`; command-line flags are available as
temporary overrides.

## Workshop user model

For every participant, the `users` operation creates:

1. A local user in a Yandex Identity Hub User Pool.
2. A personal folder in an existing cloud.
3. The `ydb.editor` role for that user on the personal folder.
4. The `resource-manager.clouds.member` role on the cloud, allowing console access.

Names are deterministic. With `YC_WORKSHOP_PREFIX=ydb` and `YC_START_INDEX=1`,
the first login, folder, database, and dedicated-mode VPC are
`ydb_001@<domain>`, `ydb-f-001`, `ydb-db-001`, and `ydb-vpc-001`. Each name is
derived independently from the common prefix and participant index. Before
creating anything, the tool lists existing users and folders and stops the
entire batch if any target name already exists.

The tool obtains the organization ID from `YC_CLOUD_ID`. It then reuses the
pool selected by `YC_USERPOOL_ID`, or finds one by `YC_USERPOOL_NAME`. If no
pool exists, it is created only when `YC_CREATE_USERPOOL=true`. Its actual login
domain is read from the API response, so `YC_USER_DOMAIN` is normally left
empty.

Two YDB creation modes are available:

- `create-ydb` creates dedicated databases and their VPC/subnets.
- `create-ydb-serverless` creates Serverless databases without VPC resources.

Both modes honor `YC_DRY_RUN=true`, check existing databases before mutation,
and can target only the folders listed in `YC_CREATE_YDB_IN_FOLDERS`. Without an
explicit list, they process only folders matching
`<YC_WORKSHOP_PREFIX>-f-<index>` and ignore unrelated cloud folders.

For a batch whose accounts already exist, `create-folders` creates or reuses the
newly named personal folders and grants access without creating, changing, or
deleting User Pool users.

## Installation

Python 3.9 or newer is recommended.

```bash
python -m venv .venv
```

PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux/macOS:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`. The file is ignored by Git. Exported environment variables take
precedence over values from `.env`.

### Authentication

The preferred option is an authorized RSA key belonging to a dedicated service
account:

```dotenv
YC_SERVICE_ACCOUNT_KEY_FILE=authorized_key.json
IAM_TOKEN=
```

At startup, the tool signs a one-hour JWT locally and exchanges it for a Yandex
Cloud IAM token. Neither the private key nor the IAM token is logged. Relative
key paths are resolved from the directory where the tool is launched.

`authorized_key*.json` and the `.secrets/` directory are ignored by Git. For a
longer-lived setup, keeping the key outside the repository or in a secret store
is preferable.

A manually generated IAM token remains available as a fallback:

```dotenv
YC_SERVICE_ACCOUNT_KEY_FILE=
IAM_TOKEN=<short-lived-token>
```

If both values are set, `YC_SERVICE_ACCOUNT_KEY_FILE` takes precedence.

## Safe user provisioning workflow

Keep this setting for the first run:

```dotenv
YC_OPERATION=users
YC_DRY_RUN=true
```

Run:

```powershell
python main.py
```

The dry run performs authenticated read-only preflight checks and does not create
resources. If the configured User Pool is absent, it reports that the pool would
be created and still checks folder-name conflicts. Review the selected pool,
cloud, user range, expiration date, and any reported conflicts. Then set:

```dotenv
YC_DRY_RUN=false
```

Run `python main.py` again to create the batch.

The result is a CSV with these columns:

```text
index,user_id,username,password,folder_id,status,error
```

`status=ready` means the user, folder, and both access bindings were completed.
`status=partial` means the user exists but folder or access setup failed. Do not
send credentials to participants until every intended row is `ready`.

`YC_USER_EXPIRES_AT` is sent as the local account's `expiresAt` timestamp. It is
optional, must be a future RFC3339 value, and should be calculated for the end
of the workshop's access window.

The CSV contains plaintext passwords. It is ignored by the repository when its
name matches `created_users*.csv`, but it must still be stored and distributed
securely.

With `YC_REQUIRE_PASSWORD_CHANGE=false`, the tool imports the generated password
hash with `needChange=false` after creating each user. The password in the CSV is
then permanent and the first login does not ask the participant to replace it.
If hash import fails, the newly created user is deleted before the batch moves
on. This mode requires `organization-manager.userpools.syncAgent` for the
provisioning service account. Set `YC_REQUIRE_PASSWORD_CHANGE=true` to use the
standard temporary-password flow instead.

Passwords are generated locally with `YC_PASSWORD_LENGTH=11`, contain lowercase
and uppercase letters plus digits, avoid ambiguous characters, and contain no
hyphens. Eleven characters is the minimum for three character classes under the
workshop User Pool's current password policy.

Existing output files are not overwritten unless
`YC_OVERWRITE_OUTPUT=true` is explicitly set.

## CLI overrides

Any commonly used `.env` setting can be overridden for one invocation:

```powershell
python main.py --do users --num-users 1 --start-index 101 --dry-run
python main.py --env-file .env.staging
```

See all options:

```powershell
python main.py --help
```

## Re-provision folders for existing users

Use this when participant credentials have already been distributed and only
their cloud resources may be recreated:

```dotenv
YC_OPERATION=create-folders
YC_WORKSHOP_PREFIX=ydb-s
YC_NUM_USERS=100
YC_START_INDEX=1
YC_RESOURCE_MANIFEST_FILE=workshop_resources.csv
YC_DRY_RUN=true
```

The preflight requires every expected account, such as `ydb-s_001@<domain>`, to
exist before it creates the first folder. It never changes a user or password.
Existing target folders are reused; missing folders are created as
`ydb-s-f-001`, and access is granted again. The non-secret resource manifest
contains participant, folder, and planned database names and IDs but no
passwords. After reviewing the dry run, set `YC_DRY_RUN=false`.

The operation is resumable: target folders that already exist are reused. If a
folder quota or API rate limit is reached, the batch stops after the first 429
response. After the quota becomes available, rerun with
`YC_OVERWRITE_OUTPUT=true` (or `--overwrite-output`) to refresh the manifest.
Folders in `PENDING_DELETION` or `DELETING` may continue to consume the
`resource-manager.folders.count` quota until deletion finishes, so a temporary
quota increase may be needed when replacing a whole batch at once.

## Other operations

The existing operations remain available:

- `create-ydb`: create dedicated YDB databases and VPC resources in folders.
- `create-ydb-serverless`: create Serverless YDB databases without VPC resources.
- `create-folders`: create or reuse folders and access for existing workshop users.
- `delete-ydb`: delete YDB databases in selected folders.
- `reset-password`: reset selected or all User Pool passwords.
- `generate-load`: generate YDB CLI workload scripts.

Use the corresponding optional variables documented in `.env.example`, or pass
the existing CLI flags shown by `python main.py --help`.

### Serverless YDB settings

Select the mode and keep dry-run enabled for the first pass:

```dotenv
YC_OPERATION=create-ydb-serverless
YC_DRY_RUN=true
YC_SERVERLESS_STORAGE_SIZE_LIMIT_GB=5
YC_SERVERLESS_ENABLE_THROTTLING=true
YC_SERVERLESS_THROTTLING_RCU_LIMIT=10
YC_SERVERLESS_PROVISIONED_RCU_LIMIT=0
```

The storage limit is entered in GiB and converted to bytes for the REST API.
The default 10 RU/s throttle limits accidental consumption. Provisioned RCU is
zero by default, so provisioned-capacity billing is disabled. After reviewing
the dry-run output, set `YC_DRY_RUN=false` to create the databases.

If a folder already contains a database of the requested type, or contains a
database with the generated name, it is skipped. The two modes therefore remain
safe to rerun without creating duplicate databases.

The provisioning service account needs `ydb.editor` on the cloud (or on every
target folder) for both listing and creating databases. Dedicated mode also
needs permission to manage VPC networks and subnets. Check the
`ydb.serverlessDatabases.count` cloud quota before creating a large workshop
batch; the standard quota may be much lower than the number of participants.

### Safe YDB deletion

`delete-ydb` refuses to operate without an explicit `YC_FOLDER_IDS` list. Start
with a read-only preview:

```dotenv
YC_OPERATION=delete-ydb
YC_FOLDER_IDS=<folder-id-1>,<folder-id-2>
YC_DRY_RUN=true
YC_CONFIRM_DELETE_YDB=false
```

For the live deletion, both safety switches must be changed explicitly:

```dotenv
YC_DRY_RUN=false
YC_CONFIRM_DELETE_YDB=true
```

The CLI equivalents are `--no-dry-run --confirm-delete-ydb`. A failed database
operation makes the command exit with a non-zero status.

## Required permissions

The configured user or service-account identity must be able to:

- read the cloud and its organization;
- list and create User Pools in the organization;
- list and create local users in the target User Pool;
- list and create folders in the target cloud;
- update folder and cloud access bindings.

YDB creation additionally requires `ydb.editor` on the target cloud or folders.

For this workflow, grant the service account User Pool editor and user
administrator permissions at the organization level, and Resource Manager
administrator permissions on the target cloud. Permanent workshop passwords
also require `organization-manager.userpools.syncAgent` on the organization.

## Failure behavior

- Remote name conflicts fail before the first mutation.
- Every HTTP request has a finite timeout.
- Long-running Yandex Cloud operations are polled to completion.
- The credentials CSV is flushed after every participant.
- A per-row status records partial provisioning instead of reporting it as success.
