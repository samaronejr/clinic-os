#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '%s\n' 'https-image-harness: invalid invocation' >&2
  exit 2
}

[[ $# -eq 1 && "$1" == 'smoke' ]] || fail
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
  "$python_bin" -m ops.testing.https_image_controller smoke
