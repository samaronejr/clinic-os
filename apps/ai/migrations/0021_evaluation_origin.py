"""Bind protected engineering scores to one run and preserve review provenance."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations import _evaluation_origin_sql


class Migration(migrations.Migration):
    """Legacy protocols and votes remain unbound/unattested, never back-certified."""

    dependencies: ClassVar = [("ai", "0020_manual_pdf_units")]
    operations: ClassVar = [
        migrations.AddField(
            "evaluationprotocol",
            "domain_protocol_sha256",
            models.CharField(max_length=64, default=""),
        ),
        migrations.AddField(
            "evaluationrating",
            "review_source",
            models.CharField(max_length=16, default="unattested"),
        ),
        migrations.RunSQL(
            _evaluation_origin_sql.SQL, _evaluation_origin_sql.REVERSE_SQL
        ),
    ]
