"""Authorize every service INSERT before non-consuming rows can return."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.scheduling.migrations._capacity_insert_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Preserve the prior trigger body and posture exactly on reverse."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("scheduling", "0005_resources_templates"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
