"""Record issued row keys on a building job; seal them with unchanged table ACLs."""

from typing import ClassVar

from django.db import migrations, models

from apps.interop.migrations import _export_finalize_sql


class Migration(migrations.Migration):
    """In-flight exports without an issued-set receipt cannot become ready."""

    dependencies: ClassVar = [
        ("interop", "0005_export_pending_origins"),
        ("ai", "0023_pending_read_boundary"),
    ]
    operations: ClassVar = [
        migrations.AddField(
            model_name="organizationexport",
            name="build_origins",
            field=models.JSONField(null=True, editable=False),
        ),
        migrations.RunSQL(_export_finalize_sql.FORWARD, _export_finalize_sql.REVERSE),
    ]
