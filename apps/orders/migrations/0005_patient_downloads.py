"""Chain one audit event for every patient download of a released result."""

from typing import ClassVar

from django.db import migrations

from apps.orders.migrations._patient_download_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Additive functions only; the released-source resolver is unchanged."""

    dependencies: ClassVar = [("orders", "0004_matched_patient_authority")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
