#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '%s\n' 'image-contract-gate: invalid invocation' >&2
  exit 2
}

[[ $# -eq 4 && "$1" == '--sha' && "$3" == '--inputs' ]] || fail
readonly sha="$2"
readonly inputs="$4"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || fail
[[ "$inputs" == '.omo/evidence/clinic-os-phase1a-final/terminal/inputs.json' ]] || fail
readonly project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly environment_root="${UV_PROJECT_ENVIRONMENT:-$project_root/.venv}"
readonly python_bin="$environment_root/bin/python"
[[ -x "$python_bin" ]] || fail

exec "$python_bin" -m ops.testing.validate_image_contract \
  --sha "$sha" --inputs "$project_root/$inputs"
