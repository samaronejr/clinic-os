"""Add the in-app portal channel to the outbox vocabulary (H-14), additively."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.ai.migrations._state_batch import SchemaStateBatch

CHANNELS = ("sms", "email", "whatsapp", "video")
PORTAL_CHANNELS = (*CHANNELS, "portal")


def _contract(channels: tuple[str, ...]) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=models.Q(
            models.Q(
                ("kind", "communication"),
                ("payload_digest", ""),
                ("channel__in", channels),
            ),
            models.Q(
                ("channel", ""),
                ("kind", "action"),
                ("payload_digest__regex", "^[0-9a-f]{64}$"),
            ),
            _connector="OR",
        ),
        name="comms_operation_kind_contract",
    )


class Migration(migrations.Migration):
    """Widen the channel choices and the kind contract; no row changes."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("comms", "0006_action_operations"),
    ]
    _schema_operations: ClassVar[Sequence[Operation]] = [
        migrations.AlterField(
            model_name="integrationoperation",
            name="channel",
            field=models.CharField(
                blank=True,
                choices=[
                    ("sms", "SMS"),
                    ("email", "Email"),
                    ("whatsapp", "WhatsApp"),
                    ("video", "Video"),
                    ("portal", "Portal"),
                ],
                max_length=32,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="integrationoperation",
            name="comms_operation_kind_contract",
        ),
        migrations.AddConstraint(
            model_name="integrationoperation",
            constraint=_contract(PORTAL_CHANNELS),
        ),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        SchemaStateBatch(
            database_operations=_schema_operations,
            state_operations=_schema_operations,
        ),
    ]
