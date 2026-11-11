"""Publish organization export v2: export v1 plus the governed-import relations."""

from typing import ClassVar

from django.db import migrations

from apps.interop.migrations import _export_v2_sql


class Migration(migrations.Migration):
    """Replace the export reader with the v2 relation set; the reverse restores v1."""

    dependencies: ClassVar = [("interop", "0002_external_api_export")]
    operations: ClassVar = [
        migrations.RunSQL(_export_v2_sql.FORWARD, _export_v2_sql.REVERSE),
    ]
