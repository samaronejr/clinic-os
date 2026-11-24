"""Bind in-flight claims to the claimant session; hold legacy in-flight sends."""

from typing import ClassVar

from django.db import migrations

from apps.comms.migrations._session_claim_sql import LEGACY_IN_FLIGHT_SQL, SQL


class Migration(migrations.Migration):
    """One transaction: resolvers, guard and the legacy in-flight pass; no reverse."""

    dependencies: ClassVar = [("comms", "0017_inflight_claim")]
    operations: ClassVar = [
        migrations.RunSQL(SQL),
        migrations.RunSQL(LEGACY_IN_FLIGHT_SQL),
    ]
