"""Move claimed or sent message attempts only through their resolvers."""

from typing import ClassVar

from django.db import migrations

from apps.comms.migrations._settled_attempt_sql import SQL


class Migration(migrations.Migration):
    """One transaction: guard, claim and closure resolvers; no reverse."""

    dependencies: ClassVar = [("comms", "0019_mac_proof")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
