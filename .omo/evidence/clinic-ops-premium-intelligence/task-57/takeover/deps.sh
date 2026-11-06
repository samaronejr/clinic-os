#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /home/samarone/Documents/clinic-ops-new-roadmap/clinic-os-wt/cpi-e-t57-r2
export PATH=/home/samarone/.local/bin:/usr/local/bin:/usr/bin:/bin
export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
export DJANGO_SETTINGS_MODULE=config.settings.test CLINIC_DATA_MODE=synthetic
uv sync --locked --all-groups
uv run --frozen --no-sync --no-env-file python manage.py compilemessages -l pt_BR
printf 'DEPS_AND_MESSAGES_PASS\n'
