"""Limit the clinic agent queue to automation administrators; reversible."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _agent_staff_authority_sql


class Migration(migrations.Migration):
    """Replace one resolver body; grants, owner and policies are untouched."""

    dependencies: ClassVar = [("ai", "0005_evaluation")]
    operations: ClassVar = [
        migrations.RunSQL(
            _agent_staff_authority_sql.SQL,
            reverse_sql=_agent_staff_authority_sql.REVERSE_SQL,
        )
    ]
