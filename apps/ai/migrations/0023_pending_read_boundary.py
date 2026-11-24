"""Original-scope point-of-read consent for every pending-output relation."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.ai.migrations import _pending_read_sql
from apps.ai.migrations._state_batch import SchemaStateBatch

_ORIGIN_FIELDS: list[Operation] = [
    migrations.AddField(
        model_name="aiartifact",
        name="source_ref",
        field=models.UUIDField(null=True, editable=False),
    ),
    migrations.AddField(
        model_name="aiartifact",
        name="source_acceptance",
        field=models.UUIDField(null=True, editable=False),
    ),
]


class Migration(migrations.Migration):
    """No table grants or audience expansion; old unbound sources fail closed."""

    dependencies: ClassVar = [
        ("ai", "0022_guide_read_scope"),
        ("identity", "0060_totp_enroll_no_key_lock"),
        ("interop", "0005_export_pending_origins"),
    ]
    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=_ORIGIN_FIELDS, state_operations=_ORIGIN_FIELDS
        ),
        migrations.RunSQL(_pending_read_sql.SQL, _pending_read_sql.REVERSE_SQL),
    ]
