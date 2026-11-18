"""Lock the TOTP device FOR NO KEY UPDATE while verifying a code."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import migrations

from apps.identity.migrations._totp_verify_lock_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Replace one function in place; owner and grants carry over.

    Reversible on an install without enterprise data, as 0051-0058 are.
    """

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0058_lock_modes_and_gates"),
    ]

    operations: ClassVar[list[Any]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
