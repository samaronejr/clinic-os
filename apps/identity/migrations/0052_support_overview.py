"""Add the masked support overview resolver; no table, grant or policy changes."""

from __future__ import annotations

from typing import ClassVar

from django.db import migrations

from apps.identity.migrations._support_overview_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Additive SECURITY DEFINER function; the reverse drops it exactly."""

    dependencies: ClassVar = [("identity", "0051_enterprise_governance")]
    operations: ClassVar = [migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL)]
