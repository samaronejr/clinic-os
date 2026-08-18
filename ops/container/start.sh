#!/usr/bin/env sh
set -eu

[ "${CLINIC_PROCESS_PURPOSE:-}" = "web" ] || exit 64
exec "$@"
