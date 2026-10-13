"""Correct the recorded claim result type while preserving lifecycle rows."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from ._lifecycle_authority_sql import CLAIM_REPAIR


class Migration(migrations.Migration):
    """Replace only the read-only token claim with its declared text result."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0020_lifecycle_writer_authority"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(
            "SET LOCAL ROLE clinic_resolver;\n" + CLAIM_REPAIR + "\nRESET ROLE;"
        ),
    ]
