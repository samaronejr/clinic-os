"""Historical uploader provenance never substitutes for current clinic authority."""

from typing import ClassVar

from django.db import migrations

from apps.orders.migrations._current_result_authority_sql import SQL


class Migration(migrations.Migration):
    """Upgrade existing result policies without replacing retained source rows."""

    dependencies: ClassVar = [("orders", "0002_accountable_result_tasks")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
