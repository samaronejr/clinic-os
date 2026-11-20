"""Require clinician-entered units for PDF lab results at the native filing edge."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _manual_pdf_unit_sql


class Migration(migrations.Migration):
    """Replace only the review resolver; keep the existing privilege contract."""

    dependencies: ClassVar = [("ai", "0019_evaluation_rollout_guard")]
    operations: ClassVar = [
        migrations.RunSQL(
            _manual_pdf_unit_sql.SQL,
            reverse_sql=_manual_pdf_unit_sql.REVERSE_SQL,
        )
    ]
