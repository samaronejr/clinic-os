"""Original-scope point-of-read consent for every pending-output relation."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations import _pending_read_sql


class Migration(migrations.Migration):
    """No table grants or audience expansion; old unbound sources fail closed."""

    dependencies: ClassVar = [
        ("ai", "0022_guide_read_scope"),
        ("identity", "0060_totp_enroll_no_key_lock"),
        ("interop", "0005_export_pending_origins"),
    ]
    operations: ClassVar = [
        migrations.AddField(
            model_name="aiartifact",
            name="source_ref",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.AddField(
            model_name="aiartifact",
            name="source_acceptance",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunSQL(_pending_read_sql.SQL, _pending_read_sql.REVERSE_SQL),
    ]
