"""Preserve professional records without exposing them through revoked roles."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._physician_current_membership_sql import SQL


class Migration(migrations.Migration):
    """Require active canonical membership for profile and evidence visibility."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0028_savedview_current_membership"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]
