"""Keep saved views private when clinic memberships are retained after revocation."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._savedview_current_membership_sql import SQL


class Migration(migrations.Migration):
    """Require current membership for reads, inserts and archive transitions."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0027_session_revoke_scope"),
    ]

    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]
