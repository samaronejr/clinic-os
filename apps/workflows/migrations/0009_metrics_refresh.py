"""Admit only the reviewed catalog's typed local refresh step."""

from typing import ClassVar

from django.db import migrations

from apps.workflows.migrations._metrics_refresh_sql import (
    RECEIPT_SQL,
    REVERSE_RECEIPT_SQL,
    REVERSE_SQL,
    SQL,
)


class Migration(migrations.Migration):
    """Existing workflow runs keep their original immutable version."""

    dependencies: ClassVar = [
        ("workflows", "0008_release_and_urgent"),
        ("analytics", "0001_initial"),
    ]
    operations: ClassVar = [
        migrations.RunSQL(
            SQL,
            reverse_sql=(REVERSE_SQL),
        ),
        migrations.RunSQL(
            RECEIPT_SQL,
            reverse_sql=REVERSE_RECEIPT_SQL,
        ),
    ]
