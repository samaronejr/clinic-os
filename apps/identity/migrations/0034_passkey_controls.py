"""Extend existing lifecycle retirement and install current-policy factor controls."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._passkey_policy_sql import POLICY_SQL
from apps.identity.migrations._passkey_retirement_sql import RETIREMENT_SQL


class Migration(migrations.Migration):
    """Retain old lifecycle function ACLs while adding explicit passkey authority."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0033_passkey_sessions"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(POLICY_SQL),
        migrations.RunSQL(RETIREMENT_SQL),
    ]
