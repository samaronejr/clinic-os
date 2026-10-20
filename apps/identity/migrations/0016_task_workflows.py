"""Add an unissued v2 candidate without upgrading any existing membership."""

from __future__ import annotations

from typing import ClassVar

from django.db import migrations, models

from apps.tenancy.migration_operations import AddConstraint

from ._workflow_eligibility_sql import SQL


class Migration(migrations.Migration):
    """Default every existing row to v1; lifecycle issuance remains separate."""

    dependencies: ClassVar = [("identity", "0015_saved_views")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="userclinicrole",
            name="workflow_bundle_version",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        AddConstraint(
            model_name="userclinicrole",
            constraint=models.CheckConstraint(
                condition=models.Q(workflow_bundle_version__in=(1, 2)),
                name="identity_workflow_bundle_version",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="rolegrant", name="identity_rolegrant_remove_only"
        ),
        AddConstraint(
            model_name="rolegrant",
            constraint=models.CheckConstraint(
                condition=models.Q(effect="remove", bundle_version__in=(1, 2)),
                name="identity_rolegrant_remove_only",
            ),
        ),
        migrations.RunSQL(SQL),
    ]
