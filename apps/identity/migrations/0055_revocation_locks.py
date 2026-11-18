"""Take the per-user rotation lock on every per-user session revocation."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import migrations

from apps.identity.migrations._revocation_lock_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Replace five functions in place; owners and grants carry over.

    Reversible on an install without enterprise data, as 0051-0054 are.
    """

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0054_atomic_session_rotation"),
    ]

    operations: ClassVar[list[Any]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
