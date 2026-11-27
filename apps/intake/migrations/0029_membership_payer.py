"""Add a nullable catalog reference without rewriting protected patient facts."""

from typing import ClassVar

from django.db import migrations, models

from apps.insurance.migrations._membership_sql import INTAKE_SQL
from apps.tenancy.migrations._resumable_schema import GuardedMigration


class Migration(GuardedMigration):
    """Repeatable SQL, irreversible metadata linkage; rollback is a restore."""

    dependencies: ClassVar = [
        ("intake", "0028_messages_operation"),
        ("insurance", "0001_revenue_cycle"),
    ]
    operations: ClassVar = [
        migrations.RunSQL(
            INTAKE_SQL,
            state_operations=[
                migrations.AddField(
                    model_name="insurancemembership",
                    name="payer",
                    field=models.ForeignKey(
                        "insurance.payer",
                        on_delete=models.PROTECT,
                        null=True,
                        blank=True,
                    ),
                )
            ],
        )
    ]
