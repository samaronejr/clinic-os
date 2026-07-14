# Clinic OS foundation runbook

This runbook covers the source-backed local foundation surface. It does not
authorize a production deployment. Security boundaries are in
[SECURITY.md](SECURITY.md), and contribution gates are in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Prerequisites

- Docker Engine with the Compose plugin
- `uv` and a supported Python version (3.12 or 3.13)
- `curl`
- optional validation tools: `actionlint` and Terraform

The compose service is PostgreSQL 16. The repository's default local port is
5432; stop conflicting local services or use an isolated Compose override in
automation. Never reuse the example credentials outside a disposable local
environment.

## First local start

Create an ignored local environment file and replace every placeholder with a
unique development value:

```sh
cp .env.example .env
set -a
. ./.env
set +a
```

Keep each `CLINIC_*_PASSWORD` synchronized with the matching password component
in `APP_DATABASE_URL`, `MIGRATION_DATABASE_URL`, and
`TEST_SUPERUSER_DATABASE_URL`. Use shell-safe local values when sourcing the
file. Compose reads `.env` itself, but Make and direct `uv` commands need the
variables exported into the current shell as shown above.

Start PostgreSQL, wait for its health check, bootstrap the fixed roles and the
pre-created test database, apply migrations as `clinic_owner`, and verify the
runtime posture:

```sh
docker compose up -d --wait db
make db-bootstrap
make migrate
make db-posture
```

`db-bootstrap` is idempotent and applies the same role/database posture to the
development and pre-created test databases. `make migrate` intentionally
substitutes `MIGRATION_DATABASE_URL` for the Django connection; never migrate
with the `clinic_app` runtime role.

Start the web process as the runtime role:

```sh
APP_DATABASE_URL="$APP_DATABASE_URL" uv run python manage.py runserver 127.0.0.1:8000
```

From a second shell:

```sh
curl --fail --silent --show-error http://127.0.0.1:8000/healthz
curl --fail --silent --show-error http://127.0.0.1:8000/readyz
```

Both return `{"status": "ok"}` when the process and database are ready.
`/healthz` never queries the database; `/readyz` runs `SELECT 1` and returns
HTTP 503 with `{"status": "unavailable"}` when the database probe fails.

## Tests and quality gates

The repository gate is:

```sh
make ci
```

It performs a locked dependency sync, idempotent database bootstrap, owner
migration, runtime posture check, Ruff lint and formatting checks, strict mypy,
the complete test/coverage suite, and a local dependency audit. Tests use the
pre-created database and explicitly exercise `clinic_app` runtime connections;
test database creation and migration remain owner responsibilities.

For a focused recent-verification check after bootstrap/migration:

```sh
uv run pytest --reuse-db tests/test_stepup_policy.py tests/test_stepup_challenge.py tests/test_stepup_sessions.py tests/test_stepup_telemetry.py tests/test_stepup_ui.py
```

Optional repository validation, without provisioning infrastructure:

```sh
actionlint .github/workflows/ci.yml
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform validate
```

Never run Terraform plan/apply as part of this foundation runbook.

## Stop and reseed

Stop containers while preserving local database data:

```sh
docker compose down
```

To rebuild a disposable local database from zero:

```sh
docker compose down -v
docker compose up -d --wait db
make db-bootstrap
make migrate
make db-posture
```

`docker compose down -v` permanently deletes the Compose database volume. Use
it only for disposable local data after confirming no needed work exists in
that volume. It is not a production recovery procedure.

## Troubleshooting

### Bootstrap or posture fails

1. Run `docker compose ps` and confirm the `db` service is healthy.
2. Confirm the shell exported all variables from the ignored `.env` file.
3. Confirm `POSTGRES_CONTAINER` is either unset (auto-detected by Make) or the
   exact container ID/name, and `POSTGRES_PORT` is the PostgreSQL port inside
   the container.
4. Re-run `make db-bootstrap`; it is designed to be idempotent.
5. Run `make db-posture`. Its expected summary identifies `clinic_app` as the
   runtime role, `clinic_owner` as `NOCREATEDB`, and the test database as
   pre-created.

Do not fix posture failures by granting owner, superuser, BYPASSRLS, schema
ownership, or direct `identity_user` access to `clinic_app`.

### Migrations or schema checks fail

- Verify `MIGRATION_DATABASE_URL` names `clinic_owner` and the same database as
  the runtime URL, then run `make migrate`.
- Check for uncommitted model changes with
  `uv run python manage.py makemigrations --check --dry-run`.
- Run `uv run pytest --reuse-db tests/test_schema_policy.py tests/test_tenancy_rls.py`.
  The tests require the exact tenant-table set, FORCE RLS, and one fail-closed
  policy per table. A newly introduced tenant model must be added to the
  migration/RLS matrix; weakening the test is not remediation.
- Use `uv run python manage.py showmigrations` for diagnosis. Do not edit an
  already-shipped migration to hide drift.

### Authentication or tenant access returns 403

- Confirm the user is active and has a `UserClinicRole` for the selected
  organization and clinic.
- Confirm the signed session contains the expected `active_org_id`. The
  middleware intentionally rejects missing, malformed, or non-member choices.
- Do not query `identity_user` with `clinic_app`. Authentication must pass
  through `auth_lookup`, and request rehydration through `load_current_user`.
- Re-run the identity, middleware, and resolver tests before changing role or
  function grants.

### TOTP or step-up loops

- Confirm the user has a confirmed TOTP device owned by that exact user.
- Password login intentionally clears prior device/freshness state. Complete
  ordinary TOTP verification again.
- Recent verification expires after the configured window (300 seconds by
  default) and rejects future or malformed timestamps. Re-enter a current OTP
  at `/auth/step-up/`; do not modify session storage manually.
- Unsafe or authentication-loop `next` targets are intentionally replaced by
  the safe default. A user with no confirmed device receives a fail-closed
  response instead of an enrollment bypass.

### Audit verification fails

Stop mutation-capable application work, preserve the payload-free verification
evidence, and follow the private incident process in
[SECURITY.md](SECURITY.md). Do not repair, delete, truncate, or resequence audit
rows. Compare database roles, functions, triggers, and migration state against
the repository before deciding on recovery.

## Secret rotation and incidents

For a suspected credential exposure, revoke or rotate the affected secret in
the authoritative deployment system, restart dependent processes, verify
database role posture and authentication, and invalidate affected sessions.
Do not paste old/new credentials, OTP seeds, database URLs, patient identifiers,
or clinical data into issues, logs, evidence files, or chat. Local example
credentials are disposable only; production rotation requires the provider's
approved procedure and a recorded incident timeline.

## Backup, restore, and PITR placeholder

No production database has been provisioned by this repository, and no tested
backup/restore command is available here. Before launch, the infrastructure
owner must write and rehearse a provider-specific runbook covering encrypted
automated backups, at least the approved retention window, restore into an
isolated environment, role/function/policy verification, application smoke
tests, audit-chain verification, recovery-point/recovery-time evidence, and
cutover/rollback authorization.

The Terraform skeleton enables an RDS backup-retention/PITR hook, but
`terraform validate` is not a restore test. Do not invent `pg_dump`, AWS, or
Terraform apply commands during an incident.

Source anchors: [Makefile](../Makefile), [Compose service](../docker-compose.yml),
[health views](../apps/core/views.py), [database posture](../ops/db/posture.py),
and [Terraform boundary](../terraform/README.md).
