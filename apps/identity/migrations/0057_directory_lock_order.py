"""Lock SCIM users' rows, and retirement's rows, before any session key."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import migrations

from apps.identity.migrations._directory_lock_order_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Replace two functions in place; owners and grants carry over.

    Reversible on an install without enterprise data, as 0051-0056 are.
    """

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0056_session_lock_order"),
    ]

    operations: ClassVar[list[Any]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
