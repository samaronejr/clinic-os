"""The owner's global AI switch stops the front-desk agent, as it stops the gateway.

The enqueue, turn, verify, state and finish bridges select the clinic's enabled
front_desk policy; they now also require the enabled global switch. Python
admission already refuses while it is off; PostgreSQL refuses on its own too.
"""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations._front_desk_switch_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Replace the five policy-bound bridges without grant or signature changes."""

    dependencies: ClassVar = [("ai", "0012_approval_attestation")]
    operations: ClassVar = [migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL)]
