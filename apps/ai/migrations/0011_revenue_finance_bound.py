"""Bind AI-06 agent rows to current Finance automation in PostgreSQL as well.

Agent staff without automation.finance neither read nor write revenue agent
rows. A revenue vote needs the approver's current automation.finance, and
tiss.manage snapshots approve revenue proposals only. No table or column is
added; reversing is supported on an empty synthetic database.
"""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _revenue_bound_sql


class Migration(migrations.Migration):
    """Restrictive policies, resolver branches and the vote guard change together."""

    dependencies: ClassVar = [("ai", "0010_revenue_cycle")]
    operations: ClassVar = [
        migrations.RunSQL(
            _revenue_bound_sql.SQL, reverse_sql=_revenue_bound_sql.REVERSE_SQL
        ),
    ]
