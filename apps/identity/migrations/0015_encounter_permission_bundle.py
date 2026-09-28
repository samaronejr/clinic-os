"""Add ``encounter.open_unscheduled`` as bundle v2 without touching frozen v1."""

from typing import ClassVar

from django.db import migrations, models

from ._encounter_permission_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Allow version-2 subtractions of the new permission only."""

    dependencies: ClassVar = [("identity", "0014_scheduling_policy")]

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
