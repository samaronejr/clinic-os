"""Isolate template v2 validation from the encounter binding guard."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from ._template_v2_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Preserve v1 rows and immutable publication history."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("ehr", "0012_addendum_receipt_open_encounter"),
        ("identity", "0009_clinic_configuration_policy"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
