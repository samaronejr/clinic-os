"""Encrypted result sources are irreversible; rollback = restore."""

from typing import ClassVar

from django.conf import settings
from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.orders.migrations import (
    _access_sql,
    _audit_sql,
    _guard_sql,
    _matching_sql,
    _patient_sql,
    _task_sql,
)
from apps.orders.migrations._schema import MODELS


class ProtectedSchema(migrations.SeparateDatabaseAndState):
    """Publish the encrypted schema and its least-privilege contract together."""

    atomic = True
    reversible = False


GRANTS = (
    "".join(
        f"GRANT SELECT ON clinic_app.{table} TO clinic_resolver;"
        for table in _access_sql.TABLES
    )
    + """
GRANT INSERT ON clinic_app.orders_resultmatch,clinic_app.orders_resultquarantine,
 clinic_app.orders_resultworkitem,clinic_app.workflows_task TO clinic_resolver;
"""
)
CONTRACT = _access_sql.render(
    GRANTS
    + _access_sql.FUNCTIONS
    + _guard_sql.FUNCTIONS
    + _task_sql.FUNCTIONS
    + _matching_sql.FUNCTIONS
    + _audit_sql.FUNCTIONS
    + _patient_sql.FUNCTIONS
    + _access_sql.POSTURE
    + _access_sql.POLICIES
    + _guard_sql.triggers(_access_sql.TABLES)
)


class Migration(migrations.Migration):
    """No plaintext conversion, and no reverse path that can erase evidence."""

    atomic = False
    dependencies: ClassVar = [
        ("identity", "0038_reception_permission"),
        ("intake", "0025_survivor_confirmations"),
        ("ehr", "0014_longitudinal_record"),
        ("workflows", "0004_task_completion_gate"),
        ("audit", "0005_clinic_metadata_v2"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations: ClassVar[list[Operation]] = [
        ProtectedSchema(
            database_operations=[*MODELS, migrations.RunSQL(CONTRACT)],
            state_operations=MODELS,
        )
    ]
