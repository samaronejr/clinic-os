"""Released charges resolve against the represented patient, not posted ids."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.billing.migrations._delegate_charges_sql import SQL


class Migration(migrations.Migration):
    """Preserve charge output and all existing ACLs."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("billing", "0009_protected_fields"),
        ("intake", "0014_delegate_sessions"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
