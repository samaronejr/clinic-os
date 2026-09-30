"""Bind addendum save receipts to an open encounter (plan item 27, round 3).

Additive: one ``CREATE OR REPLACE`` of the receipt branch of
``ehr_addendum_guard``; no table, column, grant or policy changes. The
reverse restores the exact 0011 body.
"""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from ._addendum_open_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Refuse a new addendum receipt unless its encounter is open."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("ehr", "0011_encounter_addenda"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
