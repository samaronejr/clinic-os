"""Admit only source-backed system tasks; retain the original staff guard."""

from typing import ClassVar

from django.conf import settings
from django.db import migrations, models
from django.db.models import deletion

from apps.orders.migrations._task_sql import WORKFLOW_BRIDGE


class Migration(migrations.Migration):
    """A null staff creator is legitimate only with immutable source provenance."""

    dependencies: ClassVar = [
        ("workflows", "0004_task_completion_gate"),
        ("orders", "0001_identity_safe_results"),
    ]
    operations: ClassVar = [
        migrations.AlterField(
            model_name="task",
            name="created_by",
            field=models.ForeignKey(
                settings.AUTH_USER_MODEL,
                null=True,
                on_delete=deletion.PROTECT,
                related_name="+",
            ),
        ),
        migrations.RunSQL(
            "SET LOCAL ROLE clinic_resolver;" + WORKFLOW_BRIDGE + "RESET ROLE;"
        ),
    ]
