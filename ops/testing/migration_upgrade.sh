#!/usr/bin/env bash
# Base-to-candidate migration upgrade gate.
#
# Builds a disposable database on the job's claimed PostgreSQL, migrates it
# with the *base* revision, seeds representative data through base service
# entrypoints, migrates it forward with the candidate, then verifies data
# preservation, protected-field transformation, FORCE RLS, tenant denial,
# audit integrity, grants, and migration drift.
#
# Required environment (from ci-postgres.env):
#   POSTGRES_CONTAINER POSTGRES_PASSWORD POSTGRES_USER POSTGRES_PORT
#   MIGRATION_DATABASE_URL APP_DATABASE_URL TEST_SUPERUSER_DATABASE_URL
#   GITHUB_WORKSPACE RUNNER_TEMP
# Usage: migration_upgrade.sh <base_sha>
# Set UPGRADE_FIXTURE_SWEEP=1 to require per-table mutation coverage before PASS.
set -euo pipefail

readonly base_sha="${1:?base SHA required}"
readonly project_root="${GITHUB_WORKSPACE:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)}"
readonly work_root="${RUNNER_TEMP:-/tmp}/migration-upgrade"
readonly base_worktree="$work_root/base"
upgrade_db="upgrade_rc_$$"
readonly seed_file="$work_root/seed.json"

mkdir -p "$work_root"
# Do not publish a previous run's receipts if this rehearsal fails.
rm -f "$seed_file" "$work_root/seed.pending" "$work_root/verdict.json" \
    "$work_root/verdict.pending" "$work_root/ai-assertions.json"

# Derive upgrade-DB DSNs from the claimed environment: same role
# credentials, host, port and TLS parameters, different database name.
upgrade_dsn() {
    local url="$1" path="${1%%\?*}" query=""
    case "$url" in
        *\?*) query="?${url#*\?}" ;;
    esac
    printf '%s/%s%s' "${path%/*}" "$upgrade_db" "$query"
}

psql_super() {
    docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" \
        -e CLINIC_OWNER_PASSWORD -e CLINIC_APP_PASSWORD \
        -e CLINIC_SUPER_PASSWORD \
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
    exit $rc
}
trap cleanup EXIT

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

readonly seed_state="$project_root/ops/testing/migration_upgrade_seed_state.py"
export UPGRADE_BASE_SHA="$base_sha"
export UPGRADE_BASE_OBSERVATION="$secret_dir/base.json"
export UPGRADE_SEED_COUNTS="$secret_dir/base-counts.json"
export UPGRADE_SEED_WRITES="$secret_dir/seed-writes.json"
export UPGRADE_ROWS_HELPER="$project_root/ops/testing/migration_upgrade_rows.py"
export UPGRADE_DATABASE_RECREATED=0

base_manage() (
    cd "$base_worktree"
    export APP_DATABASE_URL="$owner_dsn"
    export CLINIC_DATA_MODE=synthetic CLINIC_SECRET_BACKEND=synthetic-file
    export CLINIC_SECRET_DIR="$secret_dir" DJANGO_SETTINGS_MODULE=config.settings.base
    uv run --frozen --no-sync --no-env-file python manage.py "$@"
)

create_and_migrate_base() {
    owner_dsn="$(upgrade_dsn "$MIGRATION_DATABASE_URL")"
    app_dsn="$(upgrade_dsn "$APP_DATABASE_URL")"
    export UPGRADE_ADMIN_DATABASE_URL="$(upgrade_dsn "$TEST_SUPERUSER_DATABASE_URL")"
    echo "== create upgrade database $upgrade_db =="
    psql_super -d postgres -qc "CREATE DATABASE $upgrade_db TEMPLATE template0"
    db_created=1
    psql_super -d "$upgrade_db" \
        -v database_name="$upgrade_db" -v app_schema="clinic_app" \
        -f - < "$project_root/ops/db/bootstrap.sql"
    echo "== migrate at base revision =="
    base_manage migrate --no-input
    base_manage shell -c 'import os,runpy; runpy.run_path(os.environ["UPGRADE_SEED_STATE"])["observe_base"]()'
    base_manage shell -c 'import os,runpy; runpy.run_path(os.environ["UPGRADE_ROWS_HELPER"])["capture_base_counts"]()'
}

seed_base() {
    echo "== seed representative data at base revision =="
    UPGRADE_SEED_FILE="$seed_file" UPGRADE_HELPER_ROOT="$project_root" \
        base_manage shell < "$project_root/ops/testing/migration_upgrade_seed.py"
}

export UPGRADE_SEED_STATE="$seed_state"
create_and_migrate_base
if seed_base; then
    :
else
    rc=$?
    # Only publication I/O failure gets a retry. Never retry a guard or
    # fixture failure, and never reuse or drop a database we did not create.
    if [ "$rc" != 74 ] || [ "$db_created" != 1 ]; then
        exit "$rc"
    fi
    psql_super -d postgres -qc "DROP DATABASE $upgrade_db"
    db_created=0
    upgrade_db="${upgrade_db}_retry"
    rm -f "$UPGRADE_BASE_OBSERVATION" "$UPGRADE_SEED_COUNTS" \
        "$seed_file" "$work_root/seed.pending"
    export UPGRADE_DATABASE_RECREATED=1
    create_and_migrate_base
    seed_base
fi
base_manage shell -c 'import os,runpy; runpy.run_path(os.environ["UPGRADE_ROWS_HELPER"])["verify_seed_inventory"]()'

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

echo "== migrate forward with the candidate =="
(cd "$project_root" && env \
    APP_DATABASE_URL="$owner_dsn" \
    CLINIC_DATA_MODE="synthetic" \
    CLINIC_SECRET_BACKEND="synthetic-file" \
    CLINIC_SECRET_DIR="$secret_dir" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    uv run --frozen --no-sync --no-env-file python manage.py migrate --no-input)

# Provider MAC secrets enter the database only by COPY from the secret store.
echo "== provision provider MAC secrets =="
(cd "$project_root" && env \
    APP_DATABASE_URL="$owner_dsn" \
    CLINIC_DATA_MODE="synthetic" \
    CLINIC_SECRET_BACKEND="synthetic-file" \
    CLINIC_SECRET_DIR="$secret_dir" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    uv run --frozen --no-sync --no-env-file python manage.py provision_comms_mac_secrets)

echo "== verify preserved data, transform, and boundaries =="
export UPGRADE_AI_COVERAGE="$secret_dir/ai-coverage.json"
(cd "$project_root" && env \
    APP_DATABASE_URL="$owner_dsn" \
    CLINIC_DATA_MODE="synthetic" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    uv run --frozen --no-sync --no-env-file python manage.py shell -c \
    'import json, os; from pathlib import Path
from ops.testing.migration_upgrade_ai_verify import altered_ai_tables
from ops.testing.migration_upgrade_rewrite_graph import (
    validate_default_declarations, validate_transform_declarations,
)
validate_default_declarations()
validate_transform_declarations()
Path(os.environ["UPGRADE_AI_COVERAGE"]).write_text(json.dumps(sorted(altered_ai_tables())))')
if [ -n "${UPGRADE_WAVE_H_DIFFERENTIAL:-}" ]; then
    (cd "$project_root" && env \
        APP_DATABASE_URL="$owner_dsn" \
        CLINIC_DATA_MODE="synthetic" \
        CLINIC_SECRET_BACKEND="synthetic-file" \
        CLINIC_SECRET_DIR="$secret_dir" \
        DJANGO_SETTINGS_MODULE="config.settings.base" \
        UPGRADE_APP_DATABASE_URL="$app_dsn" \
        UPGRADE_VERIFIED_FILE="$secret_dir/verified.json" \
        UPGRADE_SEED_FILE="$seed_file" \
        uv run --frozen --no-sync --no-env-file python \
        tests/infra/wave_h_entry_differential.py "$UPGRADE_WAVE_H_DIFFERENTIAL")
fi
(cd "$project_root" && env \
    APP_DATABASE_URL="$owner_dsn" \
    CLINIC_DATA_MODE="synthetic" \
    CLINIC_SECRET_BACKEND="synthetic-file" \
    CLINIC_SECRET_DIR="$secret_dir" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    UPGRADE_APP_DATABASE_URL="$app_dsn" \
    UPGRADE_BASE_SHA="$base_sha" \
    UPGRADE_VERIFIED_FILE="$secret_dir/verified.json" \
    UPGRADE_SEED_FILE="$seed_file" \
    uv run --frozen --no-sync --no-env-file python manage.py shell \
        < "$project_root/ops/testing/migration_upgrade_verify.py")

echo "== confirm no migration drift =="
(cd "$project_root" && env \
    APP_DATABASE_URL="$owner_dsn" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    uv run --frozen --no-sync --no-env-file python manage.py makemigrations --check --dry-run)

python3 "$project_root/ops/testing/migration_upgrade_verdict.py" \
    "$seed_file" "$base_sha" "$upgrade_db" "$secret_dir/verified.json" \
    "$UPGRADE_DATABASE_RECREATED"
# Check the published artifact, not just the writer's in-memory object.
if ! cmp -s "$work_root/verdict.json" "$secret_dir/verified.json"; then
    rm -f "$work_root/verdict.json"
    echo "published upgrade verdict disagrees with the verified base histories" >&2
    exit 1
fi
echo "migration-upgrade: PASS"
