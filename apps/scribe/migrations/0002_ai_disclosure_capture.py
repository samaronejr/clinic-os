"""Bind recording admission and ingest to current AI-use disclosure."""

from typing import ClassVar

from django.db import migrations

from ._ai_disclosure_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Keep legacy guards while adding encounter-specific disclosure checks."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("scribe", "0001_initial"),
        ("teleconsult", "0007_consented_capture"),
    ]
    operations: ClassVar[list[migrations.operations.base.Operation]] = [
        migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL),
    ]
