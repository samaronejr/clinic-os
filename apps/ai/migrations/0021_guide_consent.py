"""Bind financial AI inputs to their own guide; do not grant access to consent."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _guide_consent_sql


class Migration(migrations.Migration):
    """Install only a scoped predicate; no columns, grants or backfill."""

    dependencies: ClassVar = [
        ("ai", "0020_manual_pdf_units"),
        ("insurance", "0002_distinct_reverse_accessors"),
        ("consent", "0006_rnds_exchange_purpose"),
    ]
    operations: ClassVar = [
        migrations.RunSQL(_guide_consent_sql.SQL, _guide_consent_sql.REVERSE_SQL),
    ]
