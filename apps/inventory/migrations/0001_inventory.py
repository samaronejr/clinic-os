"""Publish encrypted Inventory fields, guards and grants atomically."""

from typing import ClassVar

from django.db import migrations

from apps.inventory.migrations._inventory_sql import SQL
from apps.inventory.migrations._schema import MODELS, ProtectedSchema


class Migration(migrations.Migration):
    """Irreversible protected schema; rollback requires a restore."""

    atomic = False
    dependencies: ClassVar = [
        ("identity", "0044_inventory_permissions"),
        ("ehr", "0015_specialty_packs"),
    ]
    operations: ClassVar = [
        ProtectedSchema(
            receipt="inventory_commandreceipt_recorded_by_id_8a2e3a71",
            database_operations=[*MODELS, migrations.RunSQL(SQL)],
            state_operations=MODELS,
        ),
    ]
