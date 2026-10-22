"""Add the queue/room permission without changing arrival or movement authority."""

from typing import ClassVar

from django.db import migrations

from ._reception_permission_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Keep released appointment permissions intact."""

    dependencies: ClassVar = [("identity", "0037_passkey_controls")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
