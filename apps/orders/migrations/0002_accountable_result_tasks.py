"""Every match or quarantine creates one durable, version-bound workflow task."""

from typing import ClassVar

from django.db import migrations

from apps.orders.migrations import _task_sql


class Migration(migrations.Migration):
    """No corrected source can silently keep an earlier source's review state."""

    dependencies: ClassVar = [
        ("orders", "0001_identity_safe_results"),
        ("workflows", "0005_result_source_tasks"),
    ]
    operations: ClassVar = [migrations.RunSQL(_task_sql.TRIGGERS)]
