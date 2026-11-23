"""Permit subtraction from the unissued workflow successor without changing v1."""

from typing import ClassVar

from django.db import migrations

from apps.identity.migrations._workflow_bundle_subtractions_sql import SQL


class Migration(migrations.Migration):
    """Keep the catalog's released legacy branch exactly as installed."""

    dependencies: ClassVar = [("identity", "0016_task_workflows")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
