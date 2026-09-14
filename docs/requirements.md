# Project requirements

## Purpose

The project is a Python CLI for preparing isolated Yandex Cloud resources for
workshop participants. Configuration is read from a dotenv file and may be
overridden with command-line flags.

## Safety and authentication

- Authentication uses either an authorized service-account key or a short-lived
  IAM token. The key is preferred when both are configured.
- Secret files, generated passwords, local environments, and runtime artifacts
  must be excluded from Git.
- Every HTTP request has a finite timeout.
- Mutating batch operations provide a read-only dry-run where applicable.
- Destructive YDB deletion requires explicit folder IDs and a separate
  confirmation switch.
- A failed remote operation makes the command exit with a non-zero status.

## Workshop naming

`YC_WORKSHOP_PREFIX` defines the shared namespace. For participant `001`:

- User: `<prefix>_001@<user-pool-domain>`
- Folder: `<prefix>-f-001`
- Serverless or dedicated YDB: `<prefix>-db-001`
- Dedicated-mode VPC: `<prefix>-vpc-001`

Names are derived from the prefix and participant index. The deprecated
`YC_USER_PREFIX` name may be accepted as an alias, but conflicting old and new
values must fail validation.

## User provisioning

The `users` operation:

1. Resolves the cloud organization and finds or explicitly creates a User Pool.
2. Validates the complete target username and folder range before mutation.
3. Creates deterministic local users and copy-friendly random passwords.
4. Optionally imports a permanent password hash with first-login rotation
   disabled.
5. Creates a personal folder and grants the configured folder role.
6. Grants `resource-manager.clouds.member` for management-console access.
7. Streams user IDs, usernames, passwords, folder IDs, statuses, and errors to
   a protected CSV.

User creation is limited to 100 accounts per invocation. Existing target names
must fail the preflight before the first mutation.

## Resources for existing users

The `create-folders` operation is intended for accounts whose credentials have
already been distributed. It must:

- require the complete expected user range to exist before mutation;
- never create, update, delete, or reset users;
- create missing personal folders and reuse existing target folders;
- grant the configured folder role and cloud membership;
- write a non-secret resource manifest without passwords;
- stop promptly after a quota or rate-limit response so it can be resumed later.

## YDB provisioning

The `create-ydb-serverless` operation creates one Serverless database per
selected workshop folder without VPC resources. User-facing storage limits are
converted from GiB to bytes, and throttling and provisioned-RCU limits are
configurable.

The `create-ydb` operation creates a dedicated database. It reuses a suitable
VPC with subnets in all required zones or creates a named VPC and three subnets.

Both operations:

- process only folders matching the workshop naming scheme unless explicit
  folder IDs are supplied;
- reject explicitly selected folders that do not match the scheme;
- list existing databases before mutation;
- skip a folder containing a database of the requested type or generated name;
- support dry-run without creating networks, subnets, or databases;
- poll asynchronous operations and report failures.

## Other operations

- `delete-ydb` previews and deletes databases only in explicitly selected
  folders and requires confirmation for live deletion.
- `reset-password` resets explicitly selected users or all users in a User Pool
  and streams the new credentials to CSV.
- `generate-load` creates executable, batched YDB CLI scripts for dedicated
  databases with storage groups.

## Release quality

- Python 3.9 and 3.12 are tested in CI.
- Ruff linting, bytecode compilation, and unit tests run on every push and pull
  request.
- The README documents installation, configuration, permissions, safe dry-run
  workflows, output files, and failure behavior.
