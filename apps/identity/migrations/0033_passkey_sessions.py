"""Bind verified passkeys to retained sessions; substitution defaults off."""

from collections.abc import Sequence
from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._passkey_assertion_sql import ASSERTION_SQL
from apps.identity.migrations._passkey_session_sql import PASSKEY_SESSION_SQL


class Migration(migrations.Migration):
    """Retain TOTP verification and add a mutually exclusive passkey binding."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0032_passkeys"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.AddField(
            model_name="organization",
            name="passkey_step_up_enabled",
            field=models.BooleanField(default=False, db_default=False),
        ),
        migrations.AddField(
            model_name="sessionrecord",
            name="verification_passkey",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="identity.passkey",
            ),
        ),
        migrations.AddConstraint(
            model_name="sessionrecord",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(verification_device__isnull=True)
                    | models.Q(verification_passkey__isnull=True)
                ),
                name="identity_session_single_verification",
            ),
        ),
        migrations.RunSQL(ASSERTION_SQL),
        migrations.RunSQL(PASSKEY_SESSION_SQL),
    ]
