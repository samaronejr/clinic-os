"""Fence retained human acceptance with its original evaluated invocation."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations import _retained_effect_sql


class Migration(migrations.Migration):
    """Reuse invocation provenance; no tenant table or runtime write grant."""

    dependencies: ClassVar = [
        ("ai", "0015_front_desk_rollout"),
        ("scribe", "0005_note_drafts"),
        ("analytics", "0001_initial"),
        ("retention", "0005_transient_purge"),
    ]
    operations: ClassVar = [
        migrations.AddField(
            model_name="aiinvocation",
            name="effect_xid",
            field=models.CharField(max_length=20, blank=True, default=""),
        ),
        migrations.RunSQL(
            _retained_effect_sql.SQL,
            reverse_sql=_retained_effect_sql.REVERSE_SQL,
        ),
    ]
