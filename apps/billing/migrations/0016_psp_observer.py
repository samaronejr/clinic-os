"""Give claimed PSP effects an independent, lookup-only observer."""

from typing import ClassVar

from django.db import migrations, models

from apps.billing.migrations._psp_observer_sql import SQL


class Migration(migrations.Migration):
    """Preserve all requester, outbox, evidence and audit identities on upgrade."""

    dependencies: ClassVar = [
        ("billing", "0015_transfer_confirmation"),
        ("comms", "0011_agent_replies"),
    ]
    operations: ClassVar = [
        migrations.AddField(
            model_name="psppolicy",
            name="observer_verifier",
            field=models.BinaryField(max_length=32, null=True, editable=False),
        ),
        migrations.RunSQL(SQL),
    ]
