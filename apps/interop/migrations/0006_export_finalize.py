"""Record issued row keys on a building job; seal them with unchanged table ACLs."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.interop.migrations import _export_finalize_sql

_ORIGIN_FIELD: list[Operation] = [
    migrations.AddField(
        model_name="organizationexport",
        name="build_origins",
        field=models.JSONField(null=True, editable=False),
    )
]


class Migration(migrations.Migration):
    """In-flight exports without an issued-set receipt cannot become ready."""

    dependencies: ClassVar = [
        ("interop", "0005_export_pending_origins"),
        ("ai", "0023_pending_read_boundary"),
    ]
    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=_ORIGIN_FIELD, state_operations=_ORIGIN_FIELD
        ),
        migrations.RunSQL(_export_finalize_sql.FORWARD, _export_finalize_sql.REVERSE),
    ]
