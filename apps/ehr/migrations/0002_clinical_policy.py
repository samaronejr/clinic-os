"""Enforce clinical predicates independently of the web and service layers."""

from typing import ClassVar

from django.db import migrations

from ._clinical_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Install FORCE RLS, immutable bindings and narrow runtime grants."""

    dependencies: ClassVar = [("ehr", "0001_initial")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
