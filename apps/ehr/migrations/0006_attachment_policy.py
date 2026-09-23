"""Enforce attachment quarantine predicates independently of services."""

from typing import ClassVar

from django.db import migrations

from ._attachment_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Install FORCE RLS, transition bindings and narrow runtime grants."""

    dependencies: ClassVar = [("ehr", "0005_clinical_attachment")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
