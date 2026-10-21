"""Historical uploader provenance never substitutes for current clinic authority."""

from typing import ClassVar

from django.db import migrations

from apps.orders.migrations._access_sql import CURRENT_RESULT_READ_SQL, render


class Migration(migrations.Migration):
    """Upgrade existing result policies without replacing retained source rows."""

    dependencies: ClassVar = [("orders", "0002_accountable_result_tasks")]
    operations: ClassVar = [
        migrations.RunSQL(
            "SET LOCAL ROLE clinic_resolver;"
            + render(CURRENT_RESULT_READ_SQL)
            + "RESET ROLE;"
        )
    ]
