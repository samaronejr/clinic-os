"""Add the appointment lifecycle policy to append-only clinic configuration."""

from typing import ClassVar

from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive columns with database defaults; existing snapshots keep defaults."""

    dependencies: ClassVar = [("identity", "0013_permission_bundles")]

    operations: ClassVar = [
        migrations.AddField(
            model_name="clinicconfiguration",
            name="self_booking_requires_approval",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name="clinicconfiguration",
            name="hold_ttl_minutes",
            field=models.PositiveSmallIntegerField(db_default=10, default=10),
        ),
        migrations.AddConstraint(
            model_name="clinicconfiguration",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("hold_ttl_minutes__gte", 1), ("hold_ttl_minutes__lte", 60)
                ),
                name="identity_config_hold_ttl_bounds",
            ),
        ),
    ]
