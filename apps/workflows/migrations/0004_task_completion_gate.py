"""Forward-install stored task completion and waiting receipt guards."""

from typing import ClassVar

from django.db import migrations

from apps.workflows.migrations._task_completion_gate_sql import SQL


class Migration(migrations.Migration):
    """Keep existing receipt IDs, fences and publication pins intact."""

    dependencies: ClassVar = [("workflows", "0003_execution_receipts_and_retry")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
