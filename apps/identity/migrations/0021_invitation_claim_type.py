"""Correct the recorded claim result type while preserving lifecycle rows."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._invitation_claim_type_sql import SQL


class Migration(migrations.Migration):
    """Replace only the read-only token claim with its declared text result."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0020_lifecycle_writer_authority"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(SQL),
    ]
