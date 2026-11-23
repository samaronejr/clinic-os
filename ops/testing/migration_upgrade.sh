#!/usr/bin/env bash
# Base-to-candidate migration upgrade gate.
#
# Builds a disposable database on the job's claimed PostgreSQL, migrates it
# with the *base* revision, seeds representative data through base service
# entrypoints plus the base's populated Phase 1A fixture (byte/audit-chain
# witnesses), migrates it forward with the candidate while staging one
# representative synthetic row graph into every model the candidate adds
# (each at the first migration state where it exists, through historical
# models), then verifies data preservation, protected-field transformation,
# FORCE RLS, tenant denial, audit integrity, grants, every staged row's
# readback and encrypted round trip, and migration drift.
#
# Required environment (from ci-postgres.env):
#   POSTGRES_CONTAINER POSTGRES_PASSWORD POSTGRES_USER POSTGRES_PORT
#   MIGRATION_DATABASE_URL APP_DATABASE_URL TEST_SUPERUSER_DATABASE_URL
#   GITHUB_WORKSPACE RUNNER_TEMP
# Usage: migration_upgrade.sh <base_sha>
set -euo pipefail

readonly base_sha="${1:?base SHA required}"
readonly project_root="${GITHUB_WORKSPACE:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)}"
readonly work_root="${RUNNER_TEMP:-/tmp}/migration-upgrade"
readonly base_worktree="$work_root/base"
readonly upgrade_db="upgrade_rc_$$"
readonly seed_file="$work_root/seed.json"
readonly domain_seed_file="$work_root/domain-seed.json"
readonly witness_file="$work_root/witness-before.json"
: "${TEST_SUPERUSER_DATABASE_URL:?clinic_super URL required for the witness census}"

mkdir -p "$work_root"

# Derive upgrade-DB DSNs from the claimed environment: same role
# credentials, host, port and TLS parameters, different database name.
owner_dsn="${MIGRATION_DATABASE_URL%%\?*}"
owner_query=""
case "$MIGRATION_DATABASE_URL" in
    *\?*) owner_query="?${MIGRATION_DATABASE_URL#*\?}" ;;
esac
owner_dsn="${owner_dsn%/*}/$upgrade_db$owner_query"
app_dsn="${APP_DATABASE_URL%%\?*}"
app_query=""
case "$APP_DATABASE_URL" in
    *\?*) app_query="?${APP_DATABASE_URL#*\?}" ;;
esac
app_dsn="${app_dsn%/*}/$upgrade_db$app_query"
# Service principals authenticate as the clinic_agent login (session_user is
# their registered identity); the stager writes their records over it, as
# service_principal_context does. An unset password gets a run-local one.
agent_password_generated=0
if [ -z "${CLINIC_AGENT_PASSWORD:-}" ]; then
    CLINIC_AGENT_PASSWORD="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    agent_password_generated=1
fi
export CLINIC_AGENT_PASSWORD
agent_dsn="postgresql://clinic_agent:$CLINIC_AGENT_PASSWORD@${owner_dsn#*@}"

psql_super() {
    docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" \
        -e CLINIC_OWNER_PASSWORD -e CLINIC_APP_PASSWORD \
        -e CLINIC_SUPER_PASSWORD -e CLINIC_AGENT_PASSWORD \
        "$POSTGRES_CONTAINER" \
        psql -v ON_ERROR_STOP=1 -h localhost -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" "$@"
}

# Cleanup is armed before any resource exists so every failure path drops
# only what this run created; the original exit status is preserved.
db_created=0
worktree_added=0
secret_dir="${RUNNER_TEMP:-/tmp}/migration-upgrade-secrets.$$"
cleanup() {
    rc=$?
    trap - EXIT
    if [ "$worktree_added" = 1 ]; then
        git -C "$project_root" worktree remove --force "$base_worktree" || true
    fi
    if [ "$db_created" = 1 ]; then
        psql_super -d postgres -qc "DROP DATABASE IF EXISTS $upgrade_db" || true
    fi
    rm -rf "$secret_dir"
    # The run-local clinic_agent password must not outlive the run: clear it
    # and prove the role holds no password, on success and on failure.
    if [ "$agent_password_generated" = 1 ]; then
        psql_super -d postgres -qc "ALTER ROLE clinic_agent PASSWORD NULL" || rc=1
        if [ "$(psql_super -d postgres -tAc \
            "SELECT rolpassword IS NULL FROM pg_authid WHERE rolname='clinic_agent'")" != t ]; then
            echo "migration-upgrade: clinic_agent still holds the run-local password" >&2
            rc=1
        else
            echo "migration-upgrade: run-local clinic_agent password removed"
        fi
    fi
    exit $rc
}
trap cleanup EXIT

echo "== create upgrade database $upgrade_db =="
psql_super -d postgres -qc "CREATE DATABASE $upgrade_db"
db_created=1
psql_super -d "$upgrade_db" \
    -v database_name="$upgrade_db" -v app_schema="clinic_app" \
    -f - < "$project_root/ops/db/bootstrap.sql"

echo "== check out base $base_sha =="
git -C "$project_root" worktree add --detach "$base_worktree" "$base_sha"
worktree_added=1

echo "== sync base dependencies =="
if [ "${UPGRADE_REUSE_ENVIRONMENT:-0}" = 1 ]; then
    # Reuse is safe only for an identical locked environment. This keeps local
    # constrained-host rehearsals from rebuilding the same environment.
    cmp "$base_worktree/uv.lock" "$project_root/uv.lock"
    ln -s "$(realpath "$project_root/.venv")" "$base_worktree/.venv"
else
    (cd "$base_worktree" && uv sync --locked --all-groups --quiet)
fi

# The KEK dir must live outside work_root: the workflow publishes work_root
# as an artifact and key material must never land in retained evidence.
mkdir -m 0700 -p "$secret_dir"
head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$secret_dir/tenant-kek.secret"
chmod 600 "$secret_dir/tenant-kek.secret"
# Witness attachment objects and the fixture key probe stay private too.
readonly attachment_root="$secret_dir/attachments"
mkdir -m 0700 "$attachment_root"

echo "== migrate at base revision =="
(cd "$base_worktree" && env \
    APP_DATABASE_URL="$owner_dsn" \
    CLINIC_DATA_MODE="synthetic" \
    CLINIC_SECRET_BACKEND="synthetic-file" \
    CLINIC_SECRET_DIR="$secret_dir" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    uv run --frozen --no-sync --no-env-file python manage.py migrate --no-input)

echo "== seed representative data at base revision =="
(cd "$base_worktree" && env \
    APP_DATABASE_URL="$owner_dsn" \
    CLINIC_DATA_MODE="synthetic" \
    CLINIC_SECRET_BACKEND="synthetic-file" \
    CLINIC_SECRET_DIR="$secret_dir" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    EHR_ATTACHMENT_ROOT="$attachment_root" \
    UPGRADE_APP_DATABASE_URL="$app_dsn" \
    UPGRADE_PRIVATE_DIR="$secret_dir" \
    UPGRADE_WITNESS_FILE="$witness_file" \
    UPGRADE_SEED_FILE="$seed_file" \
    UPGRADE_HELPER_ROOT="$project_root" \
    uv run --frozen --no-sync --no-env-file python manage.py shell \
        < "$project_root/ops/testing/migration_upgrade_seed.py")

echo "== install the envelope boundary, then issue the tenant DEK =="
# Bases that already ship tenancy.0003 install the boundary during the
# base migrate, and the seed already issued a DEK through the owner
# boundary; only pre-envelope bases need the dedicated install + issue.
if ! psql_super "$upgrade_db" -tAc \
    "SELECT 1 FROM clinic_app.django_migrations WHERE app='tenancy' AND name LIKE '0003%'" \
    | grep -q 1; then
    (cd "$project_root" && env \
        APP_DATABASE_URL="$owner_dsn" \
        CLINIC_DATA_MODE="synthetic" \
        DJANGO_SETTINGS_MODULE="config.settings.base" \
        uv run --frozen --no-sync --no-env-file python manage.py migrate tenancy 0003 --no-input)
    # A tenant DEK must exist before the protected-field backfill can
    # encrypt existing plaintext rows; issue it through the supported
    # owner boundary.
    (cd "$project_root" && env \
        APP_DATABASE_URL="$owner_dsn" \
        CLINIC_DATA_MODE="synthetic" \
        CLINIC_SECRET_BACKEND="synthetic-file" \
        CLINIC_SECRET_DIR="$secret_dir" \
        DJANGO_SETTINGS_MODULE="config.settings.base" \
        UPGRADE_SEED_FILE="$seed_file" \
        uv run --frozen --no-sync --no-env-file python manage.py shell <<'PYEOF'
import json
import os
from uuid import UUID

from apps.tenancy.envelope import issue_tenant_key
from django.db import connection, transaction

organization_id = UUID(
    json.loads(open(os.environ["UPGRADE_SEED_FILE"]).read())["organization_id"]
)
with transaction.atomic(), connection.cursor() as cursor:
    cursor.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
        [str(organization_id)],
    )
    issued = issue_tenant_key()
print(f"tenant DEK issued for {organization_id}: version {issued}")
PYEOF
)
fi

echo "== migrate forward with the candidate, staging every new domain =="
(cd "$project_root" && env \
    AGENT_DATABASE_URL="$agent_dsn" \
    APP_DATABASE_URL="$owner_dsn" \
    CLINIC_DATA_MODE="synthetic" \
    CLINIC_SECRET_BACKEND="synthetic-file" \
    CLINIC_SECRET_DIR="$secret_dir" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    UPGRADE_SEED_FILE="$seed_file" \
    UPGRADE_DOMAIN_SEED_FILE="$domain_seed_file" \
    uv run --frozen --no-sync --no-env-file python -m ops.testing.migration_upgrade_stages)

echo "== verify preserved data, transform, and boundaries =="
(cd "$project_root" && env \
    APP_DATABASE_URL="$owner_dsn" \
    CLINIC_DATA_MODE="synthetic" \
    CLINIC_SECRET_BACKEND="synthetic-file" \
    CLINIC_SECRET_DIR="$secret_dir" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    EHR_ATTACHMENT_ROOT="$attachment_root" \
    UPGRADE_APP_DATABASE_URL="$app_dsn" \
    UPGRADE_PRIVATE_DIR="$secret_dir" \
    UPGRADE_WITNESS_FILE="$witness_file" \
    UPGRADE_SEED_FILE="$seed_file" \
    UPGRADE_DOMAIN_SEED_FILE="$domain_seed_file" \
    uv run --frozen --no-sync --no-env-file python manage.py shell \
        < "$project_root/ops/testing/migration_upgrade_verify.py")

echo "== confirm no migration drift =="
(cd "$project_root" && env \
    APP_DATABASE_URL="$owner_dsn" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    uv run --frozen --no-sync --no-env-file python manage.py makemigrations --check --dry-run)

echo "== scan published files for live secrets =="
(cd "$project_root" && env \
    APP_DATABASE_URL="$owner_dsn" \
    CLINIC_SECRET_DIR="$secret_dir" \
    uv run --frozen --no-sync --no-env-file python -m ops.testing.migration_upgrade_leaks \
        "$work_root")

printf '{"schema_version":1,"result":"PASS","base_sha":"%s","upgrade_db":"%s"}\n' \
    "$base_sha" "$upgrade_db" > "$work_root/verdict.json"
echo "migration-upgrade: PASS"
