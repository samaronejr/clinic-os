"""Let patient sessions book service types through the shared write path."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.scheduling.migrations._patient_service_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Add session-clinic reads, the slot projection and patient INSERT authority."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("scheduling", "0009_reception_operations"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
