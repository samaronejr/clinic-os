"""Extend the current v2 evaluator without replacing bound-identity authority."""

from typing import ClassVar

from django.db import migrations

from ._inventory_permission_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Add inventory eligibility and remove-only grants to the existing bundle."""

    dependencies: ClassVar = [("identity", "0043_delegate_messages_operation")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
