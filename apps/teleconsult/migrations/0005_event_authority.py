"""Require current participant authority for new v2 event INSERTs."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from ._event_authority_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Replace only the binding guard; reverse restores its exact prior body."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("teleconsult", "0004_teleconsult_v2"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
