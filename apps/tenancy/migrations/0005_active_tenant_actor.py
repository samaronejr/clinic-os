"""Refuse inactive accounts before opening any staff tenant transaction."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.tenancy.migrations._active_tenant_actor_sql import FORWARD_SQL, REVERSE_SQL


class Migration(migrations.Migration):
    """Replace only the tenant-entry resolver, preserving its ACL and signature."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("tenancy", "0004_protected_fields"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
