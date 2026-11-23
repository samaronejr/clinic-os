"""Permit the narrow owner writer to invoke its scoped resolver reader."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._offboarding_internal_reader_sql import SQL


class Migration(migrations.Migration):
    """Keep legacy runtime writers closed and existing records unchanged."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0024_lifecycle_function_privileges"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(SQL),
    ]
