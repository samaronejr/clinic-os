"""Bind encrypted retained proposals to their original invocation."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations import _retained_proposal_sql


class Migration(migrations.Migration):
    """Add a resolver-only transaction receipt without another provenance ledger."""

    dependencies: ClassVar = [("ai", "0016_retained_effects")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="actionproposal",
            name="source_xid",
            field=models.CharField(max_length=20, blank=True, default=""),
        ),
        migrations.RunSQL(
            _retained_proposal_sql.SQL,
            reverse_sql=_retained_proposal_sql.REVERSE_SQL,
        ),
    ]
