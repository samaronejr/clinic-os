"""Enforce scoped teleconsult authority and lifecycle in PostgreSQL."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from ._teleconsult_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Install teleconsult policy only after its stored objects exist."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("teleconsult", "0001_initial"),
        ("consent", "0002_consent_policy"),
        ("ehr", "0008_finalization_policy"),
        ("comms", "0003_video_channel"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
