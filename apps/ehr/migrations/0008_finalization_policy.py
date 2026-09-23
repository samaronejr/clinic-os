"""Enforce finalization, amendment and closure transitions in the database."""

from typing import ClassVar

from django.db import migrations

from ._finalization_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Replace the draft-only guard with the full lifecycle contract."""

    dependencies: ClassVar = [("ehr", "0007_version_finalization")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
