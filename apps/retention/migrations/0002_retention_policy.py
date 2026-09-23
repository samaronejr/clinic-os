"""Enforce retention predicates independently of services."""

from typing import ClassVar

from django.db import migrations

from ._retention_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Install FORCE RLS, lifecycle bindings and narrow runtime grants."""

    dependencies: ClassVar = [("retention", "0001_retention_records")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
