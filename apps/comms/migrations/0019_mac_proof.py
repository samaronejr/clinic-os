"""Verify provider answers by a database-checked MAC; remove the bearer path."""

from typing import ClassVar

from django.db import migrations

from apps.comms.migrations._mac_proof_sql import SQL


class Migration(migrations.Migration):
    """One transaction: secret table, MAC resolvers and guards; no reverse."""

    dependencies: ClassVar = [("comms", "0018_session_claim")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
