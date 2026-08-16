#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'isolated-db: %s\n' "$1" >&2
  exit 2
}

[[ $# -eq 1 ]] || fail 'expected exactly one command: up, status, or down'
case "$1" in
  up|status|down) readonly operation="$1" ;;
  *) fail 'expected exactly one command: up, status, or down' ;;
esac

readonly project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly compose_file="$project_root/docker-compose.yml"
readonly ledger_program="$project_root/ops/testing/isolation_ledger.py"
readonly python_bin="${CLINIC_LEDGER_PYTHON:-$project_root/.venv/bin/python}"
readonly docker_bin="${CLINIC_DOCKER_BIN:-/usr/bin/docker}"

[[ "$python_bin" == /* && -x "$python_bin" ]] || fail 'invalid ledger Python path'
[[ "$docker_bin" == /* && -x "$docker_bin" ]] || fail 'invalid Docker path'
[[ -f "$compose_file" && ! -L "$compose_file" ]] || fail 'invalid Compose file'
[[ -f "$ledger_program" && ! -L "$ledger_program" ]] || fail 'invalid ledger program'

: "${CLINIC_ISOLATION_CLAIM_ID:?isolated-db: missing CLINIC_ISOLATION_CLAIM_ID}"
: "${COMPOSE_PROJECT_NAME:?isolated-db: missing COMPOSE_PROJECT_NAME}"
: "${POSTGRES_HOST_PORT:?isolated-db: missing POSTGRES_HOST_PORT}"
: "${POSTGRES_DATA_VOLUME:?isolated-db: missing POSTGRES_DATA_VOLUME}"

[[ "$CLINIC_ISOLATION_CLAIM_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] \
  || fail 'invalid claim ID'
[[ "$COMPOSE_PROJECT_NAME" =~ ^clinic_phase1a_[a-z0-9_]+$ ]] \
  || fail 'invalid Compose project'
[[ "$POSTGRES_DATA_VOLUME" =~ ^clinic_phase1a_[a-z0-9_]+$ ]] \
  || fail 'invalid PostgreSQL volume'
[[ "$POSTGRES_HOST_PORT" =~ ^[0-9]{1,5}$ ]] \
  && ((POSTGRES_HOST_PORT >= 1 && POSTGRES_HOST_PORT <= 65535)) \
  && ((POSTGRES_HOST_PORT != 5432)) \
  || fail 'invalid isolated PostgreSQL host port'
[[ "${POSTGRES_HOST_IP:-127.0.0.1}" == '127.0.0.1' ]] \
  || fail 'isolated PostgreSQL must bind IPv4 loopback'

readonly -a ledger=("$python_bin" "$ledger_program")
readonly -a compose=(
  "$docker_bin" compose
  --env-file /dev/null
  --file "$compose_file"
  --project-name "$COMPOSE_PROJECT_NAME"
)

verify_claim() {
  "${ledger[@]}" verify --refresh --claim "$CLINIC_ISOLATION_CLAIM_ID"
}

cleanup_failed_up() {
  local exit_code=$?
  trap - ERR
  printf '%s\n' \
    'isolated-db: up failed; claimed resources retained for journaled cleanup' >&2
  return "$exit_code"
}

case "$operation" in
  up)
    trap cleanup_failed_up ERR
    "${compose[@]}" config --quiet
    "${compose[@]}" up --detach --wait db
    "${ledger[@]}" reconcile
    verify_claim
    trap - ERR
    ;;
  status)
    verify_claim
    "${compose[@]}" ps db
    ;;
  down)
    verify_claim
    "${compose[@]}" down --volumes --remove-orphans
    "${ledger[@]}" reconcile
    ;;
esac
