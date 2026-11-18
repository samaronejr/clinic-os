"""Rotate session keys only from a live session, under the per-user lock."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db import migrations

from apps.identity.migrations._atomic_rotation_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Replace the two-step supersede-then-register rotation with one function.

    Reversible on an install without enterprise data (a populated one rolls
    back only by restore, as 0051-0053 do): the reverse drops only the new
    function; the earlier ``identity_session_rotate_hash`` stays installed.
    """

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0053_delegate_access_log"),
    ]

    operations: ClassVar[list[Any]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
