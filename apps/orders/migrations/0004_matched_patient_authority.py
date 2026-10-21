"""Verified routing replaces proposed patient scope for existing source authority."""

from typing import ClassVar

from django.db import migrations

from apps.orders.migrations._access_sql import (
    CURRENT_EVENT_RECEIPT_SQL,
    CURRENT_INGEST_SCOPE_SQL,
    CURRENT_RESULT_READ_SQL,
    render,
)
from apps.orders.migrations._guard_sql import CURRENT_GUARD_SQL


class Migration(migrations.Migration):
    """Repair readers, replay and corrections without changing retained history."""

    dependencies: ClassVar = [("orders", "0003_current_result_authority")]
    operations: ClassVar = [
        migrations.RunSQL(
            "SET LOCAL ROLE clinic_resolver;"
            + render(CURRENT_RESULT_READ_SQL)
            + render(CURRENT_INGEST_SCOPE_SQL)
            + render(CURRENT_EVENT_RECEIPT_SQL)
            + render(CURRENT_GUARD_SQL)
            + "RESET ROLE;"
        )
    ]
