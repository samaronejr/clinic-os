#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '%s\n' 'final-artifact-gate: invalid invocation' >&2
  exit 2
}

[[ $# -eq 8 && "$1" == '--sha' && "$3" == '--inputs' && "$5" == '--pre-f4' && "$7" == '--staging' ]] || fail
readonly sha="$2"
readonly inputs="$4"
readonly pre_f4="$6"
readonly staging="$8"
[[ "$sha" =~ ^[0-9a-f]{40}$ && "$inputs" == /* && "$pre_f4" == /* && "$staging" == /* ]] || fail
readonly project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly environment_root="${UV_PROJECT_ENVIRONMENT:-$project_root/.venv}"
readonly python_bin="$environment_root/bin/python"
readonly decision="$staging/F4-final.txt"
readonly outcome="$staging/F4-outcome.json"
[[ -x "$python_bin" ]] || fail

exec "$python_bin" -m ops.testing.final_artifact_gate \
  --sha "$sha" --inputs "$inputs" --pre-f4 "$pre_f4" \
  --decision "$decision" --outcome "$outcome"
