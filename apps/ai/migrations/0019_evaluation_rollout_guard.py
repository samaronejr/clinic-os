"""Keep each typed evaluation assignment on its own row binding."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _rollout_evaluation_sql


class Migration(migrations.Migration):
    """Preserve all seven rollout paths without a cross-relation row cast."""

    dependencies: ClassVar = [("ai", "0018_retained_transcription")]
    operations: ClassVar = [
        migrations.RunSQL(
            _rollout_evaluation_sql.SQL,
            reverse_sql=_rollout_evaluation_sql.REVERSE_SQL,
        )
    ]
