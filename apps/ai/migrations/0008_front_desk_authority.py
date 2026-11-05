"""Bind front-desk turns to published offers and current patient authority."""

from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models

from apps.ai.migrations._front_desk_authority_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Add authority receipts and replace the existing bridges without new grants."""

    dependencies: ClassVar = [("ai", "0007_front_desk")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="frontdeskthread",
            name="verified_contact",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="intake.patientcontact",
            ),
        ),
        migrations.AddField(
            model_name="frontdeskthread",
            name="verified_contact_version",
            field=models.PositiveIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="frontdeskthread",
            name="offered_turn",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ai.frontdeskturn",
            ),
        ),
        migrations.AddField(
            model_name="frontdeskthread",
            name="automation_after",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="frontdeskthread",
            name="handoff_turn",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ai.frontdeskturn",
            ),
        ),
        migrations.AddField(
            model_name="frontdeskturn",
            name="received_offer",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ai.frontdeskturn",
            ),
        ),
        migrations.RemoveConstraint(model_name="frontdeskturn", name="ai_turn_terms"),
        migrations.AddConstraint(
            model_name="frontdeskturn",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    intent__in=(
                        "",
                        "greeting",
                        "information",
                        "appointments",
                        "booking",
                        "reschedule",
                        "birth_date",
                        "accept",
                        "decline",
                        "human",
                        "urgent",
                        "clinical",
                        "unknown",
                    )
                )
                & models.Q(
                    tool__in=(
                        "",
                        "scheduling.hold",
                        "scheduling.book_approved",
                        "comms.send_approved_link",
                    )
                )
                & models.Q(
                    outcome__in=(
                        "",
                        "answered",
                        "challenge",
                        "verified",
                        "not_verified",
                        "offered",
                        "booked",
                        "no_availability",
                        "waitlist",
                        "refused",
                        "handoff",
                    )
                )
                & (
                    models.Q(state="pending", intent="", outcome="", reply__isnull=True)
                    | models.Q(state__in=("finishing", "done"), reply__isnull=False)
                    & ~models.Q(intent="")
                    & ~models.Q(outcome="")
                ),
                name="ai_turn_terms",
            ),
        ),
        migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL),
    ]
