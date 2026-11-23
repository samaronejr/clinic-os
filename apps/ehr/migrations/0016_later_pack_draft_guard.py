"""Apply pack first-use guards to replacement drafts after discarded versions."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.ehr.migrations._later_pack_draft_guard_sql import SQL


class Migration(migrations.Migration):
    """Correct the guard without reopening the bypass on rollback."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("ehr", "0015_specialty_packs"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL)]
