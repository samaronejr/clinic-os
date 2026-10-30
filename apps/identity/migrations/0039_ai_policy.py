"""Default-disabled AI policy on retained clinic configuration snapshots."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch


class Migration(migrations.Migration):
    """Add only the closed-policy storage; gateway SQL installs its guard."""

    dependencies: ClassVar = [("identity", "0038_reception_permission")]
    _schema_operations: ClassVar = [
        migrations.AddField(
            model_name="clinicconfiguration",
            name="ai_policy",
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
    ]
    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=_schema_operations,
            state_operations=_schema_operations,
        ),
    ]
