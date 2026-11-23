"""Bind hash revocation to the authenticated account that owns the session."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._session_revoke_scope_sql import SQL


class Migration(migrations.Migration):
    """Keep session tombstoning additive; identity and grants are unchanged."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0026_session_rotation_cause"),
    ]

    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]
