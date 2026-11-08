"""Extend v2 approval authority after the reserved Wave E migration numbers."""

from typing import ClassVar

from django.db import migrations

from ._insurance_permission_sql import SQL


class Migration(migrations.Migration):
    """Preserve released v1 and every current evaluator branch and subtraction."""

    dependencies: ClassVar = [("identity", "0044_inventory_permissions")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
