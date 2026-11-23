"""Resolve completion eligibility without exposing a predecessor's task row."""

from typing import ClassVar

from django.db import migrations

from apps.workflows.migrations._predecessor_eligibility_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Add a narrow eligibility boolean; never grant predecessor row access."""

    dependencies: ClassVar = [("workflows", "0001_task_workflows")]
    operations: ClassVar = [
        migrations.RunSQL(
            SQL,
            reverse_sql=(REVERSE_SQL),
        )
    ]
