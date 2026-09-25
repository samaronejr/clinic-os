"""Separate action intent from communication channels without rewriting history."""

from typing import ClassVar

from django.conf import settings
from django.db import migrations, models

from ._action_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Default legacy rows and SQL producers to communication; bind action digests."""

    dependencies: ClassVar = [
        ("comms", "0004_pending_recovery"),
        ("identity", "0014_service_principals"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.AddField(
            model_name="integrationoperation",
            name="kind",
            field=models.CharField(
                choices=[("communication", "Communication"), ("action", "Action")],
                db_default="communication",
                default="communication",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="integrationoperation",
            name="payload_digest",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=64
            ),
        ),
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
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="integrationoperation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("kind", "communication"),
                        ("payload_digest", ""),
                        ("channel__in", ("sms", "email", "whatsapp", "video")),
                    ),
                    models.Q(
                        ("channel", ""),
                        ("kind", "action"),
                        ("payload_digest__regex", "^[0-9a-f]{64}$"),
                    ),
                    _connector="OR",
                ),
                name="comms_operation_kind_contract",
            ),
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
