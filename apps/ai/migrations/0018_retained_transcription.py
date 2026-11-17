"""Fence recognized transcript provenance without changing clinician review."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _retained_transcript_sql


class Migration(migrations.Migration):
    """Reuse the original request and existing authenticated source protocol."""

    dependencies: ClassVar = [
        ("ai", "0017_retained_proposal_sources"),
    ]
    operations: ClassVar = [
        migrations.RunSQL(
            _retained_transcript_sql.SQL,
            reverse_sql=_retained_transcript_sql.REVERSE_SQL,
        ),
    ]
