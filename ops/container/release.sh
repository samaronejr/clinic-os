#!/usr/bin/env sh
set -eu

[ "${CLINIC_PROCESS_PURPOSE:-}" = "release" ] || exit 64
[ "${DJANGO_SETTINGS_MODULE:-}" = "config.settings.release" ] || exit 64
/app/.venv/bin/python -m ops.container.release
/app/.venv/bin/python manage.py showmigrations --plan
/app/.venv/bin/python manage.py migrate --plan
/app/.venv/bin/python manage.py migrate --noinput
/app/.venv/bin/python manage.py migrate --check
