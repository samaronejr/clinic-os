"""Private origin references for point-of-download consent checks."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.ai.migrations._state_batch import SchemaStateBatch

_ORIGIN_FIELD: list[Operation] = [
    migrations.AddField(
        model_name="organizationexport",
        name="pending_origins",
        field=models.JSONField(null=True, editable=False),
    )
]


class Migration(migrations.Migration):
    """Old archive bytes have no proven dependency list; require a fresh build."""

    dependencies: ClassVar = [("interop", "0004_rnds_submission")]
    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=_ORIGIN_FIELD, state_operations=_ORIGIN_FIELD
        )
    ]
