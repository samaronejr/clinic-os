#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '%s\n' 'review-gate: invalid invocation' >&2
  exit 2
}

[[ $# -eq 4 ]] || fail
case "$1" in F1|F2) readonly lane="$1" ;; *) fail ;; esac
readonly sha="$2"
readonly verdict="$3"
readonly seconds="$4"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || fail
[[ "$seconds" =~ ^[0-9]{1,4}$ ]] && ((seconds >= 1 && seconds <= 1800)) || fail
[[ "${CLINIC_REVIEW_WORKSPACE_CLAIM_ID:-}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] || fail
readonly project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
readonly environment_root="${UV_PROJECT_ENVIRONMENT:-$project_root/.venv}"
readonly python_bin="$environment_root/bin/python"
[[ "$python_bin" == /* && -x "$python_bin" && "$verdict" == /* ]] || fail

mapfile -t prepared < <(
  "$python_bin" -m ops.testing.review_gate_child prepare \
    --lane "$lane" --sha "$sha" --verdict "$verdict"
)
[[ ${#prepared[@]} -eq 5 ]] || fail
readonly codex="${prepared[0]}"
readonly clone="${prepared[1]}"
readonly codex_home="${prepared[2]}"
readonly raw_verdict="${prepared[3]}"
readonly prompt="${prepared[4]}"

CODEX_HOME="$codex_home" timeout --signal=TERM --kill-after=30s "${seconds}s" \
  "$codex" exec --ephemeral --ignore-user-config --ignore-rules \
  --skip-git-repo-check --sandbox read-only -c approval_policy=never \
  -c project_doc_max_bytes=0 -m gpt-5.6-sol \
  -c model_reasoning_effort=xhigh -C "$clone" -o "$raw_verdict" "$(<"$prompt")"

exec "$python_bin" -m ops.testing.review_gate_child finish \
  --lane "$lane" --sha "$sha" --verdict "$verdict" --raw "$raw_verdict"
