#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '%s\n' 'ci-postgres: invalid invocation' >&2
  exit 2
}

[[ $# -eq 1 ]] || fail
case "$1" in
  up|down) readonly operation="$1" ;;
  *) fail ;;
esac
readonly project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly environment_root="${UV_PROJECT_ENVIRONMENT:-$project_root/.venv}"
readonly python_bin="$environment_root/bin/python"
[[ "$python_bin" == /* && -x "$python_bin" ]] || fail
[[ "${RUNNER_TEMP:-}" == /* ]] || fail

exec env -i \
  HOME="${HOME:?}" \
  LANG=C.UTF-8 \
  PATH=/usr/bin:/bin \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONTZPATH='' \
  RUNNER_TEMP="$RUNNER_TEMP" \
  UV_PROJECT_ENVIRONMENT="$environment_root" \
  "$python_bin" -m ops.testing.ci_postgres_controller "$operation"
