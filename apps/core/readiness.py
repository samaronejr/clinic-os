"""Bounded database readiness checks for the production web role."""

from __future__ import annotations

from typing import Final

from django.db import DatabaseError, connections, transaction
from django.db.migrations.loader import MigrationLoader

STATEMENT_TIMEOUT_MS: Final = 2_000
REQUIRED_RELATIONS: Final = (
    "django_migrations",
    "django_session",
    "identity_organization",
    "identity_user",
    "identity_clinic",
    "identity_userclinicrole",
    "otp_totp_totpdevice",
    "audit_event",
    "intake_patient",
    "intake_patientclinicenrollment",
    "scheduling_availabilityblock",
    "scheduling_appointment",
)


def probe_database_ready() -> bool:
    """Return true only for the complete migrated schema under clinic_app."""
    try:
        migration_leaves = MigrationLoader(
            None,
            ignore_no_migrations=True,
        ).graph.leaf_nodes()
        with (
            transaction.atomic(),
            connections["default"].cursor() as cursor,
        ):
            cursor.execute("SET LOCAL statement_timeout = 2000")
            cursor.execute("SELECT current_user, current_schema()")
            if cursor.fetchone() != ("clinic_app", "clinic_app"):
                return False
            for relation in REQUIRED_RELATIONS:
                qualified = f"clinic_app.{relation}"
                cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (qualified,))
                if cursor.fetchone() != (True,):
                    return False
            for app_label, migration_name in migration_leaves:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM django_migrations "
                    "WHERE app = %s AND name = %s)",
                    (app_label, migration_name),
                )
                if cursor.fetchone() != (True,):
                    return False
    except DatabaseError:
        return False
    return True
