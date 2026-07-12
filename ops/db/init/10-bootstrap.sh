#!/usr/bin/env sh
set -eu

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v database_name="$POSTGRES_DB" \
  -v app_schema="clinic_app" \
  -v clinic_owner_password="$CLINIC_OWNER_PASSWORD" \
  -v clinic_app_password="$CLINIC_APP_PASSWORD" \
  -v clinic_super_password="$CLINIC_SUPER_PASSWORD" \
  -f /opt/clinic/bootstrap.sql
