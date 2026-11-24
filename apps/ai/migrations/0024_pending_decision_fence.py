"""Serialize original patient choice with pending decisions, not just displays."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _pending_decision_sql


class Migration(migrations.Migration):
    """Keep the same resolver identities, ACLs and schema state in both directions."""

    dependencies: ClassVar = [
        ("ai", "0023_pending_read_boundary"),
        ("consent", "0006_rnds_exchange_purpose"),
    ]
    operations: ClassVar = [
        migrations.RunSQL(_pending_decision_sql.SQL, _pending_decision_sql.REVERSE_SQL)
    ]
