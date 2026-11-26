"""Use the same checked, literal-setting visibility SQL on upgrades and new installs."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations._pending_read_sql import NORMALIZE_VISIBLE


class Migration(migrations.Migration):
    """Only the spelling of constant text arguments changes, not audience or state."""

    dependencies: ClassVar = [("ai", "0025_pending_export_choice_fence")]
    # The corrected 0023 definition is canonical in either direction.
    operations: ClassVar = [
        migrations.RunSQL(NORMALIZE_VISIBLE, reverse_sql=NORMALIZE_VISIBLE)
    ]
