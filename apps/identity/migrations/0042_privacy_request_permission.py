"""Add the data-subject request permission without changing any released term."""

from typing import ClassVar

from django.db import migrations

from ._privacy_permission_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Extend v2 for management; v1 bundles and existing grants stay intact."""

    dependencies: ClassVar = [("identity", "0041_retention_authority")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
