"""Run the outbox lock session as clinic_lock and close the claim/close race."""

from typing import ClassVar

from django.db import migrations

from apps.comms.migrations._lock_role_sql import SQL


class Migration(migrations.Migration):
    """One transaction: lock-role grants and resolvers; no reverse."""

    dependencies: ClassVar = [
        ("audit", "0005_clinic_metadata_v2"),
        ("comms", "0020_settled_attempt"),
    ]
    operations: ClassVar = [migrations.RunSQL(SQL)]
