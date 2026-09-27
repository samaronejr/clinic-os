"""Add task permissions without changing the frozen v1 bundle."""

from typing import ClassVar

from django.db import migrations, models

from ._task_permission_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Preserve old removals and add a separately versioned task vocabulary."""

    dependencies: ClassVar = [
        ("identity", "0015_saved_views"),
    ]

    operations: ClassVar = [
        migrations.RemoveConstraint(
            model_name="rolegrant",
            name="identity_rolegrant_remove_only",
        ),
        migrations.AddConstraint(
            model_name="rolegrant",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("bundle_version__in", (1, 2)), ("effect", "remove")
                ),
                name="identity_rolegrant_remove_only",
            ),
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
