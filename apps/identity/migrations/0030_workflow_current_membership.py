"""Preserve workflow v2 permissions while refusing retained revoked memberships."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._workflow_current_membership_sql import SQL


class Migration(migrations.Migration):
    """Keep released v1 and unissued v2 branches with current membership authority."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0029_physician_current_membership"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]
