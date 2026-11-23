"""Forward-install current execution guards on the already migrated candidate."""

from typing import ClassVar

from django.db import migrations

from apps.workflows.migrations._execution_receipts_sql import SQL


class Migration(migrations.Migration):
    """Preserve history and pins; install stricter effects and explicit retries."""

    dependencies: ClassVar = [("workflows", "0002_predecessor_eligibility")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
