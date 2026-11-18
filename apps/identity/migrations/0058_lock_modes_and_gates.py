"""Use NO KEY UPDATE on referenced rows; take the user gate before rows."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import migrations

from apps.identity.migrations._lock_modes_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Replace six functions in place; owners and grants carry over.

    Reversible on an install without enterprise data, as 0051-0057 are.
    """

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0057_directory_lock_order"),
    ]

    operations: ClassVar[list[Any]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
