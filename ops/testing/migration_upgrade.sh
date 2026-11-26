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
# Set UPGRADE_WAVE_H_DIFFERENTIAL=1 for independent native Wave H comparison.
# Consumers require the run_nonce announced by this invocation in its PASS.
set -euo pipefail
export GIT_OPTIONAL_LOCKS=0

if [[ "${1:-}" != --private-namespace ]]; then
    exec /usr/bin/python3 -I "${BASH_SOURCE[0]%/*}/migration_upgrade_namespace.py" \
        "${BASH_SOURCE[0]}" --private-namespace "$@"
fi
shift

readonly base_sha="${1:-}"
readonly runner_root="${RUNNER_TEMP:-/tmp}"
work_root="$runner_root/migration-upgrade"
readonly started_at="$EPOCHREALTIME"
readonly run_id="${UPGRADE_RUN_ID:-${started_at//./}-$$-$RANDOM}"
commit_sha="${UPGRADE_COMMIT_SHA:-unknown}"
[[ "$commit_sha" =~ ^[0-9a-f]{40}$ ]] || commit_sha=unknown
upgrade_db="upgrade_rc_$$"
readonly differential="${UPGRADE_WAVE_H_DIFFERENTIAL:-}"

# Derive upgrade-DB DSNs from the claimed environment: same role
# credentials, host, port and TLS parameters, different database name.
upgrade_dsn() {
    local url="$1" path="${1%%\?*}" query=""
    case "$url" in
        *\?*) query="?${url#*\?}" ;;
    esac
    printf '%s/%s%s' "${path%/*}" "${2:-$upgrade_db}" "$query"
}

psql_super() {
    sandboxed docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" \
        -e CLINIC_OWNER_PASSWORD -e CLINIC_APP_PASSWORD \
        -e CLINIC_SUPER_PASSWORD \
        "$POSTGRES_CONTAINER" \
        psql -v ON_ERROR_STOP=1 -h localhost -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" "$@"
}

# Cleanup is armed before any resource exists so every failure path drops
# only what this run created; the original exit status is preserved.
db_created=0
secret_dir=""
run_name=""
run_fd=""
run_identity=""
wave_db_created=0
wave_db="${upgrade_db}_wave"
wave_tree=""
differential_stage=setup
differential_report=""
verdict_ready=0
setup_detail=""
project_root=""
run_nonce=""
publication_count=0
declare -A owned_paths=()
declare -A publication_pending=()
destination_fd=""
running_published=0

remember_open_file() {
    local identity
    identity="$(stat -Lc '%d:%i' -- "/proc/$$/fd/$2")" || return 1
    owned_paths["$1"]="$identity:$run_nonce"
}

# Every child runs with the host read-only and the private run directory as the
# only writable mount, so a link planted under a name it follows cannot reach a
# writable foreign inode. The held descriptors name the run directory's entries.
declare -a sandbox_entries=()

sandboxed() {
    python3 -I "$project_root/ops/testing/migration_upgrade_sandbox.py" \
        "$$" "${run_fd:--}" "${run_name:--}" "${sandbox_entries[@]}" -- "$@"
}

write_private_key() {
    sandboxed python3 "$project_root/ops/testing/migration_upgrade_kek.py" "${1:-}"
}

require_run_identity() {
    if [[ -z "$run_identity" || "$(stat -Lc '%d:%i' -- "$secret_dir")" != "$run_identity" ]]; then
        echo "private_run_identity_mismatch" >&2
        return 1
    fi
}

unlink_private_child() {
    python3 -I "$project_root/ops/testing/migration_upgrade_custody.py" \
        unlink "$run_fd" "$run_identity" "$1" "$2"
}

mkdir_private_child() {
    python3 -I "$project_root/ops/testing/migration_upgrade_custody.py" \
        mkdir "$run_fd" "$run_identity" "$@"
}

declare -A run_entries=()
declare -A run_links=()

hold_private_dir() {
    local held
    exec {held}< "$secret_dir/$1"
    if [[ "$(stat -Lc '%d:%i' -- "/proc/$$/fd/$held")" != "$2" ]]; then
        setup_detail=private_child_replaced
        exit 1
    fi
    run_entries["$1"]="$2"
    sandbox_entries+=("$held:$1")
    printf -v "$3" '%s' "/proc/$$/fd/$held"
}

make_private_dir() {
    local made
    made="$(mkdir_private_child "$1")" || return 1
    hold_private_dir "$1" "${made##* }" "$2"
}

guard_private_tree() {
    local entry_name entry_list=()
    for entry_name in "${!run_entries[@]}"; do
        entry_list+=("$entry_name=${run_entries[$entry_name]}")
    done
    for entry_name in "${!run_links[@]}"; do
        entry_list+=("link:$entry_name"$'\t'"${run_links[$entry_name]}")
    done
    entry_list+=("mount:/dev/shm/$run_name")
    if ! python3 -I "$project_root/ops/testing/migration_upgrade_custody.py" \
        check "$run_fd" "$run_identity" "${entry_list[@]}"; then
        setup_detail=private_tree_foreign_entry
        exit 1
    fi
}

remove_run_tree() {
    require_run_identity || return 1
    # The kernel reclaims the anonymous mount at last close. There is no
    # public private-tree name to unlink, nor any teardown traversal.
    exec {run_fd}<&-
}

refuse_public_private_entries() {
    local peer
    for peer in "$root_handle"/migration-upgrade-secrets.*; do
        if [[ -e "$peer" || -L "$peer" ]]; then
            setup_detail=private_run_create_failed
            exit 1
        fi
    done
}

owns_path() {
    local identity
    [[ -n "${owned_paths[$1]:-}" ]] || return 1
    identity="$(stat -c '%d:%i' -- "$1" 2>/dev/null)" || return 1
    [[ "$identity:$run_nonce" = "${owned_paths[$1]}" ]]
}

owns_destination_lock() {
    local line
    [[ -n "$destination_fd" ]] || return 1
    while IFS= read -r line; do
        [[ "$line" == lock:*FLOCK*WRITE* ]] && return 0
    done < "/proc/$$/fdinfo/$destination_fd"
    return 1
}

publish_shell_verdict() {
    local result="${1:-FAIL}" pending fd
    publication_count=$((publication_count + 1))
    pending="$work_root/.verdict-$run_nonce-$publication_count.pending"
    set -o noclobber
    if ! exec {fd}> "$pending"; then
        set +o noclobber
        return 1
    fi
    set +o noclobber
    publication_pending["$pending"]="$fd"
    remember_open_file "$pending" "$fd" || return 1
    printf '{"schema_version":1,"result":"%s","reason":"%s","detail":"%s","run_id":"%s","run_nonce":"%s","commit_sha":"%s","started_at":"%s"}\n' \
        "$result" "$differential_stage" "$setup_detail" "$run_id" "$run_nonce" "$commit_sha" "$started_at" >&"$fd" || return 1
    owns_path "$pending" || return 1
    mv -T -- "$pending" "$work_root/verdict.json" || return 1
    unset 'publication_pending[$pending]' 'owned_paths[$pending]'
    exec {fd}>&-
}

validate_root() {
    local remaining="${runner_root#/}" prefix="" component
    [[ "$runner_root" = /* ]] || return 1
    while [ -n "$remaining" ]; do
        component="${remaining%%/*}"
        case "$component" in ""|.|..) return 1 ;; esac
        prefix="$prefix/$component"
        [[ ! -L "$prefix" && -d "$prefix" ]] || return 1
        [[ "$remaining" = */* ]] || break
        remaining="${remaining#*/}"
    done
    [[ ! -L "$work_root" ]] || return 1
}

publish_verdict() {
    python3 - "$work_root" "$base_sha" "$upgrade_db" "$1" \
        "$differential_stage" "$differential_report" "$run_id" "$commit_sha" \
        "$started_at" "$secret_dir/verdict.json" "$differential" "$run_nonce" <<'PYEOF'
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

root, base, db, result, stage, report, run_id, commit, started, attestation, differential, nonce = sys.argv[1:]
target = Path(root) / "verdict.json"
comparison = Path(report)
details = json.loads(comparison.read_text()) if comparison.is_file() else {
    "result": "FAIL",
    "projection_cases": 0,
    "probe_window_cases": 0,
    "differences": [],
    "base_reject_lane_pass_pairs": [],
}
if result == "PASS":
    verdict = json.loads(Path(attestation).read_text())
    if differential and details.get("result") != "PASS":
        raise RuntimeError("differential passing report is missing")
else:
    verdict = {"schema_version": 1, "result": "FAIL", "base_sha": base, "upgrade_db": db}
    verdict["reason"] = stage
if differential:
    verdict["differential"] = details
verdict.update(run_id=run_id, run_nonce=nonce, commit_sha=commit, started_at=started)
pending = target.with_name(f".verdict-{uuid4().hex}.pending")
with pending.open("x", encoding="utf-8") as stream:
    identity = os.fstat(stream.fileno())

    def still_owned():
        return os.path.samestat(pending.lstat(), identity)

    try:
        stream.write(json.dumps(verdict) + "\n")
        stream.flush()
        if not still_owned():
            raise RuntimeError("verdict staging file was replaced")
        pending.replace(target)
    except BaseException:
        try:
            if still_owned():
                with pending.open(encoding="utf-8") as receipt:
                    owned_receipt = os.path.samestat(os.fstat(receipt.fileno()), identity)
                    if owned_receipt and json.load(receipt).get("run_nonce") == nonce and still_owned():
                        pending.unlink()
        except (OSError, ValueError):
            pass
        raise
PYEOF
}

cleanup() {
    rc=$?
    trap - EXIT ERR
    trap '' TERM INT HUP
    if [ "$rc" != 0 ]; then
        echo "migration-upgrade: FAIL / $differential_stage${setup_detail:+: $setup_detail} (run $run_id${run_nonce:+, nonce $run_nonce})" >&2
        if [ "$differential_stage" = interrupted ] && [ "$running_published" = 0 ] &&
            owns_destination_lock; then
            # The shell can dispatch a trap before observing flock's status.
            # fdinfo proves that this descriptor, not a contender, holds it.
            verdict_ready=1
            if publish_shell_verdict RUNNING; then
                printf 'migration-upgrade: run_nonce=%s\n' "$run_nonce"
            else
                echo "verdict_destination_unwritable (run $run_id)" >&2
            fi
        fi
        if [ "$verdict_ready" = 1 ]; then
            if [ -z "$run_identity" ] ||
                [ "$differential_stage" = setup ] || [ "$differential_stage" = interrupted ]; then
                publish_shell_verdict || echo "verdict_destination_unwritable (run $run_id)" >&2
            elif ! publish_verdict FAIL; then
                publish_shell_verdict || echo "verdict_destination_unwritable (run $run_id)" >&2
            fi
        fi
    fi
    if [ "$db_created" = 1 ]; then
        psql_super -d postgres -qc "DROP DATABASE IF EXISTS $upgrade_db" || true
    fi
    if [ "$wave_db_created" = 1 ]; then
        psql_super -d postgres -qc "DROP DATABASE IF EXISTS $wave_db" || true
    fi
    local pending contents fd
    if [[ -n "$run_identity" ]] && ! remove_run_tree; then
        echo "migration-upgrade: private_run_cleanup_failed" >&2
        if [ "$rc" = 0 ]; then rc=1; fi
    fi
    for pending in "${!publication_pending[@]}"; do
        if owns_path "$pending" && [[ ! -L "$pending" ]] &&
            IFS= read -r contents < "$pending" &&
            [[ "$contents" == *'"run_nonce":"'"$run_nonce"'"'* ]] &&
            owns_path "$pending"; then
            rm -f -- "$pending" || true
        fi
        fd="${publication_pending[$pending]}"
        exec {fd}>&-
    done
    exit $rc
}
interrupt() {
    differential_stage=interrupted
    setup_detail="$1"
    exit "$2"
}
trap 'interrupt TERM 143' TERM
trap 'interrupt INT 130' INT
trap 'interrupt HUP 129' HUP
trap 'exit "$?"' ERR
trap cleanup EXIT

if [[ ! "$run_id" =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "migration-upgrade: setup: invalid run identity" >&2
    exit 1
fi
differential_stage=unsafe_destination
validate_root
differential_stage=setup
for required in mkdir realpath flock od tr mv stat; do
    command -v "$required" >/dev/null || { setup_detail="missing_$required"; exit 127; }
done
run_nonce="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
[[ "$run_nonce" =~ ^[0-9a-f]{32}$ ]]

# Pin the verified directory and lock its inode, not a replaceable lock file.
differential_stage=unsafe_destination
exec {root_fd}< "$runner_root"
root_handle="/proc/$$/fd/$root_fd"
[[ "$(realpath -e -- "$root_handle")" = "$runner_root" ]]
differential_stage=verdict_destination_unwritable
if [ ! -d "$root_handle/migration-upgrade" ]; then
    mkdir -- "$root_handle/migration-upgrade" ||
        [[ -d "$root_handle/migration-upgrade" ]] || exit 1
fi
differential_stage=unsafe_destination
validate_root
exec {destination_fd}< "$root_handle/migration-upgrade"
destination_handle="/proc/$$/fd/$destination_fd"
[[ "$(realpath -e -- "$destination_handle")" = "$work_root" ]]
validate_root
work_root="$destination_handle"

project_root="${GITHUB_WORKSPACE:-${BASH_SOURCE[0]%/*}/../..}"
requested_commit="$commit_sha"
if command -v git >/dev/null; then
    if resolved_sha="$(git -C "$project_root" rev-parse HEAD)"; then
        commit_sha="$resolved_sha"
    fi
fi
differential_stage=concurrent_run
flock -n -x -E 75 "$destination_fd"
differential_stage=verdict_destination_unwritable
# Arm failure publication before the atomic replacement. A signal delivered
# at the rename boundary already has the lock, nonce, and writable handle.
verdict_ready=1
publish_shell_verdict RUNNING
running_published=1
printf 'migration-upgrade: run_nonce=%s\n' "$run_nonce"
differential_stage=setup
for required in git tar python3 bwrap docker uv mkdir rm rmdir head od tr chmod cmp ln realpath grep env; do
    if ! command -v "$required" >/dev/null; then
        setup_detail="missing_$required"
        exit 127
    fi
done
if [ -z "${resolved_sha:-}" ]; then
    setup_detail=commit_unavailable
    exit 1
fi
if [ "$requested_commit" != unknown ] && [ "$requested_commit" != "$commit_sha" ]; then
    setup_detail=commit_mismatch
    exit 1
fi
if [ -e "$work_root/seed.pending" ] || [ -L "$work_root/seed.pending" ]; then
    differential_stage=stale_seed_pending
    echo "stale_seed_pending: refusing to remove an existing seed staging entry" >&2
    exit 1
fi
if [ -z "$base_sha" ]; then
    differential_stage=missing_base_sha
    echo "base SHA required" >&2
    exit 1
fi
readonly project_root
export PYTHONDONTWRITEBYTECODE=1
umask 077

# The public artifact directory never contains fixture material.
run_name="migration-upgrade-secrets.$run_nonce"
refuse_public_private_entries
coproc CUSTODY {
    exec python3 -I "$project_root/ops/testing/migration_upgrade_custody.py" "$run_name"
}
custody_process=$CUSTODY_PID
if ! read -r creator descriptor created_identity <&"${CUSTODY[0]}"; then
    setup_detail=private_run_create_failed
    exit 1
fi
[[ "$creator:$descriptor" =~ ^[0-9]+:[0-9]+$ && "$created_identity" =~ ^[0-9]+:[0-9]+$ ]]
exec {run_fd}< "/proc/$creator/fd/$descriptor"
secret_dir="/proc/$$/fd/$run_fd"
[[ "$(stat -Lc '%d:%i:%a' -- "$secret_dir")" = "$created_identity:700" ]]
printf '.' >&"${CUSTODY[1]}"
wait "$custody_process"
run_identity="$created_identity"
export UPGRADE_PRIVATE_RUN_IDENTITY="$run_identity"
refuse_public_private_entries
readonly seed_file="$secret_dir/seed.json"
base_worktree=""
wave_tree=""
native_dir=""
differential_report="$secret_dir/differential.json"
export UPGRADE_PRIVATE_RUN_DIR="$secret_dir"
make_private_dir tmp tmp_dir
make_private_dir uv-cache cache_dir
export TMPDIR="$tmp_dir" UV_CACHE_DIR="$cache_dir"
echo "== check out base $base_sha =="
guard_private_tree
# git writes only to the pipe; the extractor creates every entry through the
# run directory descriptor, so no destination name exists for a link to hijack.
exec {wrapper_stdin}<&0
coproc ARCHIVE { sandboxed git -C "$project_root" archive --format=tar "$base_sha" <&"$wrapper_stdin"; }
archive_process=$ARCHIVE_PID
archive_in="${ARCHIVE[0]}"
if ! extracted="$(python3 -I "$project_root/ops/testing/migration_upgrade_custody.py" \
    extract "$run_fd" "$run_identity" base <&"$archive_in")"; then
    eval "exec ${archive_in}<&-"
    wait "$archive_process" || true
    setup_detail=private_extract_failed
    exit 1
fi
eval "exec ${archive_in}<&-"
wait "$archive_process"
base_line="${extracted%%$'\n'*}"
while IFS=$'\t' read -r link_kind link_path link_target; do
    if [[ "$link_kind" == link ]]; then
        run_links["$link_path"]="$link_target"
    fi
done <<< "$extracted"
hold_private_dir base "${base_line##* }" base_worktree

echo "== sync base dependencies =="
guard_private_tree
if [ "${UPGRADE_REUSE_ENVIRONMENT:-0}" = 1 ]; then
    # Reuse is safe only for an identical locked environment. This keeps local
    # constrained-host rehearsals from rebuilding the same environment.
    sandboxed cmp "$base_worktree/uv.lock" "$project_root/uv.lock"
    venv_target="$(realpath "$project_root/.venv")"
    python3 -I "$project_root/ops/testing/migration_upgrade_custody.py" symlink \
        "$base_worktree" "${run_entries[base]}" .venv "$venv_target"
    run_links[base/.venv]="$venv_target"
else
    (cd "$base_worktree" && sandboxed uv sync --locked --all-groups --quiet)
fi

# The KEK dir must live outside work_root: the workflow publishes work_root
# as an artifact and key material must never land in retained evidence.
write_private_key "$secret_dir"

readonly seed_state="$project_root/ops/testing/migration_upgrade_seed_state.py"
export UPGRADE_BASE_SHA="$base_sha"
export UPGRADE_BASE_OBSERVATION="$secret_dir/base.json"
export UPGRADE_SEED_COUNTS="$secret_dir/base-counts.json"
export UPGRADE_SEED_WRITES="$secret_dir/seed-writes.json"
export UPGRADE_ROWS_HELPER="$project_root/ops/testing/migration_upgrade_rows.py"
export UPGRADE_DATABASE_RECREATED=0

base_manage() (
    guard_private_tree
    cd "$base_worktree"
    export APP_DATABASE_URL="$owner_dsn"
    export CLINIC_DATA_MODE=synthetic CLINIC_SECRET_BACKEND=synthetic-file
    export UPGRADE_PRIVATE_RUN_DIR="$secret_dir"
    export CLINIC_SECRET_DIR="$secret_dir" DJANGO_SETTINGS_MODULE=config.settings.base
    sandboxed uv run --frozen --no-sync --no-env-file python manage.py "$@"
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
    local rc=0
    echo "== seed representative data at base revision =="
    if [ -n "$differential" ]; then
        differential_stage=candidate_probe_case_generation
        collect_probe_cases "$project_root" "$seed_file" "$secret_dir/lane-probes.json" || rc=$?
    else
        UPGRADE_SEED_FILE="$seed_file" UPGRADE_HELPER_ROOT="$project_root" \
            base_manage shell < "$project_root/ops/testing/migration_upgrade_seed.py" || rc=$?
    fi
    return "$rc"
}

collect_probe_cases() {
    UPGRADE_HELPER_ROOT="$1" UPGRADE_SEED_FILE="$2" UPGRADE_WAVE_H_PROBE_CASES="$3" \
        UPGRADE_PROBE_DRIVER="$project_root/tests/infra/wave_h_probe_faults.py" \
        base_manage shell -c 'import os, runpy
from pathlib import Path
runpy.run_path(os.environ["UPGRADE_PROBE_DRIVER"])["run_seed"](
    Path(os.environ["UPGRADE_HELPER_ROOT"]) / "ops/testing/migration_upgrade_seed.py",
    Path(os.environ["UPGRADE_WAVE_H_PROBE_CASES"]))'
}

rehearse_wave_h() {
    local wave_sha=4f8d2454987ca1d083ead53a7501126d647a89bc
    make_private_dir native native_dir
    local native="$native_dir"
    write_private_key "$native"
    differential_stage=reference_worktree
    make_private_dir wave wave_tree
    guard_private_tree
    python3 -I "$project_root/ops/testing/migration_upgrade_custody.py" \
        empty "$wave_tree" "${run_entries[wave]}"
    sandboxed git -C "$project_root" clone --quiet --shared --no-checkout -- "$project_root" "$wave_tree"
    sandboxed git -C "$wave_tree" checkout --quiet --detach "$wave_sha"
    if sandboxed cmp -s "$wave_tree/uv.lock" "$project_root/uv.lock"; then
        venv_target="$(realpath "$project_root/.venv")"
        python3 -I "$project_root/ops/testing/migration_upgrade_custody.py" symlink \
            "$wave_tree" "${run_entries[wave]}" .venv "$venv_target"
        run_links[wave/.venv]="$venv_target"
    else
        (cd "$wave_tree" && sandboxed uv sync --locked --all-groups --quiet)
    fi
    test "$(sandboxed git -C "$wave_tree" rev-parse HEAD)" = "$wave_sha"
    test -z "$(sandboxed git -C "$wave_tree" status --porcelain --untracked-files=no)"

    differential_stage=reference_migration
    local owner_dsn app_dsn secret_dir="$native"
    owner_dsn="$(upgrade_dsn "$MIGRATION_DATABASE_URL" "$wave_db")"
    app_dsn="$(upgrade_dsn "$APP_DATABASE_URL" "$wave_db")"
    psql_super -d postgres -qc "CREATE DATABASE $wave_db TEMPLATE template0"
    wave_db_created=1
    psql_super -d "$wave_db" -v database_name="$wave_db" -v app_schema=clinic_app \
        -f - < "$wave_tree/ops/db/bootstrap.sql"
    base_manage migrate --no-input
    differential_stage=reference_probe_case_generation
    collect_probe_cases "$wave_tree" "$native/seed.json" "$native/probes.json"
    differential_stage=reference_forward_migration
    (cd "$wave_tree" && sandboxed env APP_DATABASE_URL="$owner_dsn" \
        CLINIC_DATA_MODE=synthetic CLINIC_SECRET_BACKEND=synthetic-file \
        CLINIC_SECRET_DIR="$native" DJANGO_SETTINGS_MODULE=config.settings.base \
        uv run --frozen --no-sync --no-env-file python manage.py migrate --no-input)
    APP_DATABASE_URL="$owner_dsn" UPGRADE_APP_DATABASE_URL="$app_dsn" \
        UPGRADE_ADMIN_DATABASE_URL="$(upgrade_dsn "$TEST_SUPERUSER_DATABASE_URL" "$wave_db")" \
        CLINIC_SECRET_DIR="$native" UPGRADE_SEED_FILE="$native/seed.json" \
        UPGRADE_WAVE_H_PROBE_CASES="$native/probes.json" \
        sandboxed python3 - "$native/environment.json" <<'PYEOF'
import json
import os
import sys
from pathlib import Path

keys = ("APP_DATABASE_URL", "UPGRADE_APP_DATABASE_URL", "UPGRADE_ADMIN_DATABASE_URL",
        "CLINIC_SECRET_DIR", "UPGRADE_SEED_FILE", "UPGRADE_WAVE_H_PROBE_CASES")
with Path(sys.argv[1]).open("x", encoding="utf-8") as stream:
    stream.write(json.dumps({key: os.environ[key] for key in keys}))
PYEOF
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
    for artifact in "$UPGRADE_BASE_OBSERVATION" "$UPGRADE_SEED_COUNTS" \
        "$seed_file" "$secret_dir/seed.pending" "$secret_dir/lane-probes.json"; do
        unlink_private_child "${artifact##*/}" 1
    done
    export UPGRADE_DATABASE_RECREATED=1
    create_and_migrate_base
    seed_base
fi
differential_stage=forward_rehearsal
base_manage shell -c 'import os,runpy; runpy.run_path(os.environ["UPGRADE_ROWS_HELPER"])["verify_seed_inventory"]()'

echo "== install the envelope boundary, then issue the tenant DEK =="
# Bases that already ship tenancy.0003 install the boundary during the
# base migrate, and the seed already issued a DEK through the owner
# boundary; only pre-envelope bases need the dedicated install + issue.
if ! psql_super "$upgrade_db" -tAc \
    "SELECT 1 FROM clinic_app.django_migrations WHERE app='tenancy' AND name LIKE '0003%'" \
    | grep -q 1; then
    (cd "$project_root" && sandboxed env \
        APP_DATABASE_URL="$owner_dsn" \
        CLINIC_DATA_MODE="synthetic" \
        DJANGO_SETTINGS_MODULE="config.settings.base" \
        uv run --frozen --no-sync --no-env-file python manage.py migrate tenancy 0003 --no-input)
    # A tenant DEK must exist before the protected-field backfill can
    # encrypt existing plaintext rows; issue it through the supported
    # owner boundary.
    (cd "$project_root" && sandboxed env \
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
(cd "$project_root" && sandboxed env \
    APP_DATABASE_URL="$owner_dsn" \
    CLINIC_DATA_MODE="synthetic" \
    CLINIC_SECRET_BACKEND="synthetic-file" \
    CLINIC_SECRET_DIR="$secret_dir" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    uv run --frozen --no-sync --no-env-file python manage.py migrate --no-input)

guard_private_tree
echo "== verify preserved data, transform, and boundaries =="
export UPGRADE_AI_COVERAGE="$secret_dir/ai-coverage.json"
(cd "$project_root" && sandboxed env \
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
with Path(os.environ["UPGRADE_AI_COVERAGE"]).open("x", encoding="utf-8") as stream:
    stream.write(json.dumps(sorted(altered_ai_tables())))')
if [ -n "$differential" ]; then
    rehearse_wave_h
    guard_private_tree
differential_stage=verifier_differential
    differential_rc=0
    (cd "$project_root" && sandboxed env \
        APP_DATABASE_URL="$owner_dsn" \
        CLINIC_DATA_MODE="synthetic" \
        CLINIC_SECRET_BACKEND="synthetic-file" \
        CLINIC_SECRET_DIR="$secret_dir" \
        DJANGO_SETTINGS_MODULE="config.settings.base" \
        UPGRADE_APP_DATABASE_URL="$app_dsn" \
        UPGRADE_BASE_SHA="$base_sha" \
        UPGRADE_VERIFIED_FILE="$secret_dir/verified.json" \
        UPGRADE_SEED_FILE="$seed_file" \
        UPGRADE_WAVE_H_NATIVE_ENV="$native_dir/environment.json" \
        UPGRADE_WAVE_H_PROBE_CASES="$secret_dir/lane-probes.json" \
        UPGRADE_WAVE_H_REPORT="$differential_report" \
        uv run --frozen --no-sync --no-env-file python \
        tests/infra/wave_h_entry_differential.py "$wave_tree") || differential_rc=$?
    if [ "$differential_rc" != 0 ]; then
        exit "$differential_rc"
    fi
fi
guard_private_tree
differential_stage=clean_forward_verification
(cd "$project_root" && sandboxed env \
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
(cd "$project_root" && sandboxed env \
    APP_DATABASE_URL="$owner_dsn" \
    DJANGO_SETTINGS_MODULE="config.settings.base" \
    uv run --frozen --no-sync --no-env-file python manage.py makemigrations --check --dry-run)

guard_private_tree
sandboxed python3 "$project_root/ops/testing/migration_upgrade_verdict.py" \
    "$seed_file" "$base_sha" "$upgrade_db" "$secret_dir/verified.json" \
    "$UPGRADE_DATABASE_RECREATED"
# Check the published artifact, not just the writer's in-memory object.
if ! sandboxed cmp -s "$secret_dir/verdict.json" "$secret_dir/verified.json"; then
    echo "published upgrade verdict disagrees with the verified base histories" >&2
    exit 1
fi
guard_private_tree
differential_stage=verdict_publication
publish_verdict PASS
echo "migration-upgrade: PASS"
