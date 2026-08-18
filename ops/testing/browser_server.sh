#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '%s\n' 'browser-server: invalid invocation' >&2
  exit 2
}

[[ $# -ge 1 ]] || fail
readonly operation="$1"
[[ "$operation" == 'suite' ]] || fail
shift

selected=()
if [[ $# -ge 1 && "$1" != --* ]]; then
  case "$1" in
    availability|patient|runtime-https|scheduling) selected=("$1") ;;
    *) fail ;;
  esac
  shift
fi

required=()
previous=''
while [[ $# -gt 0 ]]; do
  [[ $# -ge 2 && "$1" == '--require-suite' ]] || fail
  suite="$2"
  case "$suite" in
    availability|patient|runtime-https|scheduling) ;;
    *) fail ;;
  esac
  [[ -z "$previous" || "$previous" < "$suite" ]] || fail
  required+=("--require-suite" "$suite")
  previous="$suite"
  shift 2
done
[[ ${#selected[@]} -ge 1 || ${#required[@]} -ge 2 ]] || fail

readonly project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly environment_root="${UV_PROJECT_ENVIRONMENT:-$project_root/.venv}"
readonly python_bin="$environment_root/bin/python"
[[ "$python_bin" == /* && -x "$python_bin" ]] || fail

: "${CLINIC_BROWSER_BASE_URL:?browser-server: missing CLINIC_BROWSER_BASE_URL}"
: "${CLINIC_BROWSER_CLINIC_ID:?browser-server: missing CLINIC_BROWSER_CLINIC_ID}"
: "${CLINIC_BROWSER_EVIDENCE_ROOT:?browser-server: missing CLINIC_BROWSER_EVIDENCE_ROOT}"
: "${CLINIC_BROWSER_USERNAME:?browser-server: missing CLINIC_BROWSER_USERNAME}"
: "${CLINIC_BROWSER_PASSWORD:?browser-server: missing CLINIC_BROWSER_PASSWORD}"
: "${APP_DATABASE_URL:?browser-server: missing APP_DATABASE_URL}"
[[ "$CLINIC_BROWSER_EVIDENCE_ROOT" == /* ]] || fail

exec env -i \
  HOME="${HOME:?}" \
  LANG=C.UTF-8 \
  PATH=/usr/bin:/bin \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$project_root" \
  PYTHONTZPATH='' \
  DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:?}" \
  APP_DATABASE_URL="$APP_DATABASE_URL" \
  CLINIC_DATA_MODE=synthetic \
  CLINIC_BROWSER_BASE_URL="$CLINIC_BROWSER_BASE_URL" \
  CLINIC_BROWSER_CLINIC_ID="$CLINIC_BROWSER_CLINIC_ID" \
  CLINIC_BROWSER_EVIDENCE_ROOT="$CLINIC_BROWSER_EVIDENCE_ROOT" \
  CLINIC_BROWSER_USERNAME="$CLINIC_BROWSER_USERNAME" \
  CLINIC_BROWSER_PASSWORD="$CLINIC_BROWSER_PASSWORD" \
  CLINIC_BROWSER_ORG_ID="${CLINIC_BROWSER_ORG_ID:-}" \
  CLINIC_BROWSER_PHYSICIAN_ID="${CLINIC_BROWSER_PHYSICIAN_ID:-}" \
  CLINIC_BROWSER_PHYSICIAN_USERNAME="${CLINIC_BROWSER_PHYSICIAN_USERNAME:-}" \
  CLINIC_BROWSER_PHYSICIAN_PASSWORD="${CLINIC_BROWSER_PHYSICIAN_PASSWORD:-}" \
  "$python_bin" -m ops.testing.browser_server_supervisor \
  "$operation" "${selected[@]}" "${required[@]}"
