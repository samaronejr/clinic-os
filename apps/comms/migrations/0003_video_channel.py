"""Add the video channel for teleconsult room operations."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    """Record the room-creation channel; no schema shape changes."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("comms", "0002_integrationoperation_not_before_appointmentreminder"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.AlterField(
            model_name="integrationoperation",
            name="channel",
            field=models.CharField(
                choices=[
                    ("sms", "SMS"),
                    ("email", "Email"),
                    ("whatsapp", "WhatsApp"),
                    ("video", "Video"),
                ],
                max_length=32,
            ),
        ),
    ]
