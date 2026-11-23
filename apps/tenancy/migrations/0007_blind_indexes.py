"""Tenant-DEK HMAC indexes, matching the todo-17 envelope boundary."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.tenancy.migrations._blind_indexes_sql import INSTALL_SQL, REVERSE_SQL


class Migration(migrations.Migration):
    """Install only the fixed purpose-bound digest API; keys never leave SQL."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("tenancy", "0006_runtime_pooling")
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            INSTALL_SQL,
            REVERSE_SQL,
        )
    ]
