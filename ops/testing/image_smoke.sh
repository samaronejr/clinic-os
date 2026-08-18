#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '%s\n' 'image-smoke: invalid invocation' >&2
  exit 2
}

[[ $# -eq 3 ]] || fail
case "$1" in
  build|smoke) readonly operation="$1" ;;
  *) fail ;;
esac
[[ "$2" == '--sha' && "$3" =~ ^[0-9a-f]{40}$ ]] || fail
readonly revision="$3"
readonly project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly environment_root="${UV_PROJECT_ENVIRONMENT:-$project_root/.venv}"
readonly python_bin="$environment_root/bin/python"
[[ "$python_bin" == /* && -x "$python_bin" ]] || fail

exec env -i \
  HOME="${HOME:?}" \
  LANG=C.UTF-8 \
  PATH=/usr/bin:/bin \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONTZPATH='' \
  "$python_bin" -m ops.testing.image_smoke_controller "$operation" --sha "$revision"
