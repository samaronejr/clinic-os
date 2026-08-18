#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '%s\n' 'browser-runner: invalid invocation' >&2
  exit 2
}

[[ $# -ge 1 ]] || fail
readonly operation="$1"
shift
readonly -a controller_arguments=("$@")
case "$operation" in
  probe)
    ;;
  session)
    [[ $# -ge 4 && "$1" == '--profile' && "$3" == '--origin' ]] || fail
    case "$2" in
      host-http)
        [[ "$4" =~ ^http://phase1a-browser\.qa\.clinic-os\.test:[1-9][0-9]{0,4}$ ]] || fail
        ;;
      container-https)
        [[ "$4" == 'https://phase1a.qa.clinic-os.dev:8443' ]] || fail
        [[ $# -ge 6 && "$5" == '--ca' && "$6" == '/run/clinic-test-db-trust/db-ca.pem' ]] || fail
        shift 2
        ;;
      *) fail ;;
    esac
    shift 4
    ;;
  *) fail ;;
esac

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
  "$python_bin" -m ops.testing.browser_runner_controller "$operation" "${controller_arguments[@]}"
