"""Keep AI-04 domain effects inside their evaluated exposure permission."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations import _front_desk_rollout_sql


class Migration(migrations.Migration):
    """Extend existing turn provenance; no tenant table or runtime write grant."""

    dependencies: ClassVar = [("ai", "0014_staged_rollout")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="frontdeskturn",
            name="rollout_approval",
            field=models.ForeignKey(
                "ai.modelchangerequest",
                null=True,
                on_delete=models.PROTECT,
                related_name="+",
            ),
        ),
        migrations.AddField(
            model_name="frontdeskturn",
            name="source_xid",
            field=models.CharField(max_length=20, blank=True, default=""),
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
                        "proposed",
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
                    | models.Q(state="proposed", outcome="proposed", reply__isnull=True)
                    & ~models.Q(intent="")
                    | models.Q(state__in=("finishing", "done"), reply__isnull=False)
                    & ~models.Q(intent="")
                    & ~models.Q(outcome="")
                ),
                name="ai_turn_terms",
            ),
        ),
        migrations.RunSQL(
            _front_desk_rollout_sql.SQL,
            reverse_sql=_front_desk_rollout_sql.REVERSE_SQL,
        ),
    ]
