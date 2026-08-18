#!/usr/bin/env bash
set -euo pipefail

readonly STAGE_CLEANUP_SECONDS=120
readonly SUPERVISOR_CLEANUP_SECONDS=150
readonly OUTER_FORCE_KILL_SECONDS=180
readonly project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly environment_root="${UV_PROJECT_ENVIRONMENT:-$project_root/.venv}"
readonly python_bin="$environment_root/bin/python"

fail() {
  printf '%s\n' 'f2-gate: invalid invocation' >&2
  exit 2
}

[[ $# -eq 4 && "$1" == '--sha' && "$3" == '--evidence' ]] || fail
readonly SHA="$2"
readonly evidence="$4"
[[ "$SHA" =~ ^[0-9a-f]{40}$ && "$evidence" == /* && -x "$python_bin" ]] || fail
[[ "$(git rev-parse HEAD)" == "$SHA" ]] || fail
[[ -z "$(git status --porcelain=v1 --untracked-files=all)" ]] || fail
readonly tree_sha="$(git rev-parse "${SHA}^{tree}")"
readonly evidence_parent="$(dirname -- "$evidence")"
readonly evidence_name="$(basename -- "$evidence")"
readonly work="$evidence_parent/.${evidence_name}.records"
[[ -d "$evidence_parent" && ! -L "$evidence_parent" && ! -e "$work" ]] || fail
mkdir -m 700 -- "$work"

cleanup() {
  if [[ -d "$work" && ! -L "$work" ]]; then
    rm -rf -- "$work"
  fi
}
trap cleanup EXIT HUP INT TERM

readonly -a stages=(
  'timeout --signal=TERM --kill-after=120s 1800s make ci'
  'timeout 60s git diff --check cffbb1900ae2132560f20c27fcf1a514a1ef71aa.."$SHA"'
  'timeout 300s uv run ruff check .'
  'timeout 300s uv run ruff format --check .'
  'timeout 600s uv run mypy .'
  'timeout 600s uv run pip-audit --local'
  'timeout 600s uv run pytest -q tests/test_production_settings.py tests/test_release_settings.py tests/test_browser_settings.py tests/test_database_options.py tests/test_data_mode.py tests/test_product_telemetry.py tests/test_timezone_source.py'
  'timeout 600s uv run pytest -q tests/test_schema_policy.py tests/test_resolver_catalog.py tests/test_readiness.py tests/test_container_contract.py'
  'timeout 900s uv run pytest -q tests/test_patient_services.py tests/test_availability_concurrency.py tests/test_appointment_concurrency.py tests/test_appointment_transition_concurrency.py tests/test_lifecycle_lock_races.py'
  'timeout 900s uv run pytest -q tests/test_phase1_audit_contract.py tests/test_phase1_audit_migration.py tests/test_audit_append.py tests/test_audit_unicode_boundaries.py'
  'timeout 300s uv run python ops/testing/assert_foundation_history.py cffbb1900ae2132560f20c27fcf1a514a1ef71aa'
  'timeout --signal=TERM --kill-after=120s 900s ops/testing/image_contract_gate.sh --sha "$SHA" --inputs .omo/evidence/clinic-os-phase1a-final/terminal/inputs.json'
  'timeout --signal=TERM --kill-after=120s 600s ops/testing/tls_stack.sh smoke'
  'timeout --signal=TERM --kill-after=120s 600s uv run pytest -q tests/test_isolated_db_harness.py tests/test_isolation_ledger.py'
)

for index in "${!stages[@]}"; do
  stage="$work/$((index + 1))"
  mkdir -m 700 "$stage"
  printf '%s\n' "${stages[$index]}" > "$stage/argv"
  date -u +%Y-%m-%dT%H:%M:%S.%6NZ > "$stage/started"
  set +e
  SHA="$SHA" bash -ceu "${stages[$index]}" > "$stage/stdout" 2> "$stage/stderr"
  status=$?
  set -e
  date -u +%Y-%m-%dT%H:%M:%S.%6NZ > "$stage/ended"
  printf '%s\n' "$status" > "$stage/exit"
  sha256sum "$stage/stdout" | cut -d' ' -f1 > "$stage/stdout.sha256"
  sha256sum "$stage/stderr" | cut -d' ' -f1 > "$stage/stderr.sha256"
  rm -f -- "$stage/stdout" "$stage/stderr"
  ((status == 0)) || exit "$status"
done

"$python_bin" -m ops.testing.validate_f2_receipt create \
  --sha "$SHA" --tree "$tree_sha" --records "$work" --evidence "$evidence"
