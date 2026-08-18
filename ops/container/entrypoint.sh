#!/usr/bin/env sh
set -eu

case "${CLINIC_PROCESS_PURPOSE:-}" in
  web|release) ;;
  *) exit 64 ;;
esac
exec "$@"
