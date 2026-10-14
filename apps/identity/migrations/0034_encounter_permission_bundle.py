"""Add ``encounter.open_unscheduled`` as bundle v2 without touching frozen v1."""

from typing import ClassVar

from django.db import migrations

from ._encounter_permission_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Extend current membership and workflow authority with the new permission."""

    dependencies: ClassVar = [("identity", "0033_scheduling_policy")]

    operations: ClassVar = [
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
