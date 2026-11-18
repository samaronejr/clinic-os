"""Move session serialization to its own key, taken after every row lock."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import migrations

from apps.identity.migrations._session_lock_order_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Replace eight functions in place and add one lock helper.

    Reversible on an install without enterprise data, as 0051-0055 are.
    """

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0055_revocation_locks"),
    ]

    operations: ClassVar[list[Any]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
