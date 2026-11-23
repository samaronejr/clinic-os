"""Close the plan-status helper on databases that already applied AI-05."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations._care_plan_status_private_sql import SQL


class Migration(migrations.Migration):
    """Keep the internal predicate executable only by its resolver owner."""

    dependencies: ClassVar = [("ai", "0007_care_coordination")]
    operations: ClassVar = [
        migrations.RunSQL(
            SQL,
            # Corrected 0007 has the same closed ACL; never restore the exposure.
            reverse_sql=migrations.RunSQL.noop,
        )
    ]
