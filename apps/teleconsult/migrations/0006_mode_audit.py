"""Audit the patient's audio-mode transitions under patient authority."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from ._mode_audit_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Add the patient mode-audit scope and writer; reverse drops both."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("teleconsult", "0005_event_authority"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
