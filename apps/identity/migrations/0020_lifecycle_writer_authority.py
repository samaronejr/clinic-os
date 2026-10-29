"""Correct the installed lifecycle writers without widening table privileges."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from ._lifecycle_authority_sql import FORWARD_SQL


class Migration(migrations.Migration):
    """Retain existing data and repair purpose-bound authority additively."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0019_identity_lifecycle"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(FORWARD_SQL)]
