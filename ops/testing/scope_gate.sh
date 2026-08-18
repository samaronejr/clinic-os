#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '%s\n' 'scope-gate: invalid invocation' >&2
  exit 2
}

[[ $# -eq 6 && "$1" == '--sha' && "$3" == '--inputs' && "$5" == '--evidence' ]] || fail
readonly sha="$2"
readonly inputs="$4"
readonly evidence="$6"
[[ "$sha" =~ ^[0-9a-f]{40}$ && "$inputs" == /* && "$evidence" == /* ]] || fail
readonly project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly environment_root="${UV_PROJECT_ENVIRONMENT:-$project_root/.venv}"
readonly python_bin="$environment_root/bin/python"
[[ -x "$python_bin" ]] || fail

exec "$python_bin" -m ops.testing.scope_gate \
  --sha "$sha" --inputs "$inputs" --evidence "$evidence"
