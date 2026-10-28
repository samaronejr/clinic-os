"""Default-disabled AI policy on retained clinic configuration snapshots."""

from typing import ClassVar

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add only the closed-policy storage; gateway SQL installs its guard."""

    dependencies: ClassVar = [("identity", "0038_reception_permission")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="clinicconfiguration",
            name="ai_policy",
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
    ]
