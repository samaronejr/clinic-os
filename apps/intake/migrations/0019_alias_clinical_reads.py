"""Resolve retained clinical histories without rewriting their bindings."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.ehr.migrations._patient_alias_read_sql import SQL


class Migration(migrations.Migration):
    """Expand only same-clinic care reads across human-adjudicated aliases."""

    dependencies: ClassVar = [("intake", "0018_survivor_writes")]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL)]
