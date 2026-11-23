"""Verified routing replaces proposed patient scope for existing source authority."""

from typing import ClassVar

from django.db import migrations

from apps.orders.migrations._matched_patient_authority_sql import SQL


class Migration(migrations.Migration):
    """Repair readers, replay and corrections without changing retained history."""

    dependencies: ClassVar = [("orders", "0003_current_result_authority")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
