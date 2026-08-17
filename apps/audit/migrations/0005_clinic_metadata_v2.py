"""Install reversible clinic-aware version-two audit append functions."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.audit.migrations._v2_append_sql import (
    INSTALL_V2_APPEND_SQL,
    REMOVE_V2_APPEND_SQL,
)
from apps.audit.migrations._v2_boundary_sql import (
    INSTALL_V2_BOUNDARY_SQL,
    REMOVE_V2_BOUNDARY_SQL,
)


class Migration(migrations.Migration):
    """Version both append paths while preserving the version-one wrappers."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("audit", "0004_raw_audit_string_boundaries"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            sql=INSTALL_V2_APPEND_SQL,
            reverse_sql=REMOVE_V2_APPEND_SQL,
        ),
        migrations.RunSQL(
            sql=INSTALL_V2_BOUNDARY_SQL,
            reverse_sql=REMOVE_V2_BOUNDARY_SQL,
        ),
    ]
