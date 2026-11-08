"""Close the plan-status helper on databases that already applied AI-05."""

from typing import ClassVar

from django.db import migrations


class Migration(migrations.Migration):
    """Keep the internal predicate executable only by its resolver owner."""

    dependencies: ClassVar = [("ai", "0007_care_coordination")]
    operations: ClassVar = [
        migrations.RunSQL(
            """
            SET LOCAL ROLE clinic_resolver;
            REVOKE ALL ON FUNCTION
                clinic_app.ai_care_plan_current(uuid,integer)
                FROM PUBLIC,clinic_app,clinic_owner,clinic_agent;
            RESET ROLE;
            """,
            # Corrected 0007 has the same closed ACL; never restore the exposure.
            reverse_sql=migrations.RunSQL.noop,
        )
    ]
