"""Lock the reused pending TOTP device FOR NO KEY UPDATE while enrolling."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import migrations

from apps.identity.migrations._totp_enroll_lock_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Replace one function in place; owner and grants carry over.

    Reversible on an install without enterprise data, as 0051-0059 are.
    """

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0059_totp_verify_no_key_lock"),
    ]

    operations: ClassVar[list[Any]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
