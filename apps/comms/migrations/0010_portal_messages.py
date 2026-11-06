"""Install the patient side of portal threads: resolvers, guard branch, audit."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.comms.migrations._portal_messages_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Additive SQL; no table or runtime grant shape changes."""

    # Both operation vocabularies (session and grant, delegate) precede the resolvers.
    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("comms", "0009_inbound_audit"),
        ("identity", "0043_delegate_messages_operation"),
        ("intake", "0028_messages_operation"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL),
    ]
