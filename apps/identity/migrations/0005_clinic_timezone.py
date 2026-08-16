"""Require an explicit IANA timezone for every clinic."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.identity.phase1a_timezone_migration import (
    backfill_clinic_timezones,
    clear_clinic_timezones,
)
from apps.scheduling.timezones import IanaTimezoneField, validate_iana_timezone


class Migration(migrations.Migration):
    """Add the clinic timezone after Todo 2 identity hardening."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0004_current_actor_acl_and_physician_catalog"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="clinic",
            name="timezone",
            field=IanaTimezoneField(
                max_length=64,
                null=True,
                validators=[validate_iana_timezone],
            ),
        ),
        migrations.RunPython(backfill_clinic_timezones, clear_clinic_timezones),
        migrations.AlterField(
            model_name="clinic",
            name="timezone",
            field=IanaTimezoneField(
                max_length=64,
                validators=[validate_iana_timezone],
            ),
        ),
        migrations.AddConstraint(
            model_name="clinic",
            constraint=models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "btrim(timezone) <> ''",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="identity_clinic_timezone_nonblank",
            ),
        ),
    ]
