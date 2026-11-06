"""Front-desk agent (todo 54): reviewed answers, thread state and turns; additive.

The three tables hold no patient text. The SQL installs their exact posture, the
guard, the principal-bound agent bridges and the message-guard branch for the
agent's replies. Reverse is for an empty synthetic database only; once turns or
answers exist, rollback = restore.
"""

import uuid
from typing import ClassVar

import django.contrib.postgres.fields
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.ai.migrations._front_desk_sql import REVERSE_SQL, SQL
from apps.ai.migrations._state_batch import SchemaStateBatch

_STATE: list[Operation] = [
    migrations.CreateModel(
        name="FrontDeskThread",
        fields=[
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            ("state", models.CharField(default="active", max_length=16)),
            (
                "handoff_reason",
                models.CharField(blank=True, default="", max_length=24),
            ),
            ("verified_until", models.DateTimeField(null=True)),
            ("challenge_attempts", models.PositiveSmallIntegerField(default=0)),
            ("challenge_until", models.DateTimeField(null=True)),
            ("offered_start", models.DateTimeField(null=True)),
            ("offered_until", models.DateTimeField(null=True)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="identity.clinic",
                ),
            ),
            (
                "conversation",
                models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="comms.conversation",
                ),
            ),
            (
                "offered_appointment",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="scheduling.appointment",
                ),
            ),
            (
                "offered_practitioner",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "resolved_by",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "session",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="intake.patientsession",
                ),
            ),
            (
                "verified_enrollment",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="intake.patientclinicenrollment",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="KnowledgeArticle",
        fields=[
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            ("key", models.CharField(max_length=40)),
            ("version", models.PositiveIntegerField()),
            ("topic", models.CharField(max_length=16)),
            ("title", models.CharField(max_length=120)),
            ("body", models.CharField(max_length=600)),
            (
                "keywords",
                django.contrib.postgres.fields.ArrayField(
                    base_field=models.CharField(max_length=40), size=12
                ),
            ),
            ("link", models.URLField(blank=True, default="")),
            ("state", models.CharField(default="draft", max_length=10)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("approved_at", models.DateTimeField(null=True)),
            ("retired_at", models.DateTimeField(null=True)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "approved_by",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="identity.clinic",
                ),
            ),
            (
                "created_by",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="FrontDeskTurn",
        fields=[
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            ("state", models.CharField(default="pending", max_length=10)),
            ("intent", models.CharField(blank=True, default="", max_length=16)),
            ("tool", models.CharField(blank=True, default="", max_length=32)),
            ("outcome", models.CharField(blank=True, default="", max_length=16)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="identity.clinic",
                ),
            ),
            (
                "inbound",
                models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="comms.message",
                ),
            ),
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "policy",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="ai.agentpolicyversion",
                ),
            ),
            (
                "reply",
                models.OneToOneField(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="comms.message",
                ),
            ),
            (
                "thread",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="turns",
                    to="ai.frontdeskthread",
                ),
            ),
            (
                "article",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="ai.knowledgearticle",
                ),
            ),
        ],
    ),
    migrations.AddConstraint(
        model_name="frontdeskthread",
        constraint=models.UniqueConstraint(
            fields=("organization", "clinic", "id"), name="ai_frontdesk_scope"
        ),
    ),
    migrations.AddConstraint(
        model_name="frontdeskthread",
        constraint=models.CheckConstraint(
            condition=models.Q(
                models.Q(
                    models.Q(("handoff_reason", ""), ("state", "active")),
                    models.Q(
                        ("state", "handoff_pending"),
                        models.Q(("handoff_reason", ""), _negated=True),
                    ),
                    models.Q(("resolved_by__isnull", False), ("state", "resolved")),
                    _connector="OR",
                ),
                ("challenge_attempts__lte", 3),
            ),
            name="ai_frontdesk_state",
        ),
    ),
    migrations.AddConstraint(
        model_name="knowledgearticle",
        constraint=models.UniqueConstraint(
            fields=("clinic", "key", "version"), name="ai_article_version"
        ),
    ),
    migrations.AddConstraint(
        model_name="knowledgearticle",
        constraint=models.UniqueConstraint(
            fields=("organization", "clinic", "id"), name="ai_article_scope"
        ),
    ),
    migrations.AddConstraint(
        model_name="knowledgearticle",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("version__gte", 1),
                (
                    "topic__in",
                    ("hours", "prices", "services", "preparation", "policies"),
                ),
                models.Q(
                    models.Q(("approved_by__isnull", True), ("state", "draft")),
                    models.Q(
                        ("approved_at__isnull", False),
                        ("approved_by__isnull", False),
                        ("state__in", ("approved", "retired")),
                    ),
                    _connector="OR",
                ),
            ),
            name="ai_article_terms",
        ),
    ),
    migrations.AddConstraint(
        model_name="frontdeskturn",
        constraint=models.UniqueConstraint(
            fields=("organization", "clinic", "id"), name="ai_turn_scope"
        ),
    ),
    migrations.AddConstraint(
        model_name="frontdeskturn",
        constraint=models.CheckConstraint(
            condition=models.Q(
                (
                    "intent__in",
                    (
                        "",
                        "greeting",
                        "information",
                        "appointments",
                        "reschedule",
                        "birth_date",
                        "accept",
                        "decline",
                        "human",
                        "urgent",
                        "clinical",
                        "unknown",
                    ),
                ),
                (
                    "tool__in",
                    (
                        "",
                        "scheduling.hold",
                        "scheduling.book_approved",
                        "comms.send_approved_link",
                    ),
                ),
                (
                    "outcome__in",
                    (
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
                    ),
                ),
                models.Q(
                    models.Q(
                        ("intent", ""),
                        ("outcome", ""),
                        ("reply__isnull", True),
                        ("state", "pending"),
                    ),
                    models.Q(
                        ("reply__isnull", False),
                        ("state__in", ("finishing", "done")),
                        models.Q(("intent", ""), _negated=True),
                        models.Q(("outcome", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
            ),
            name="ai_turn_terms",
        ),
    ),
]


class Migration(migrations.Migration):
    """Tables, guard, bridges and the agent reply branch land together."""

    dependencies: ClassVar = [
        ("ai", "0006_agent_staff_authority"),
        ("comms", "0011_agent_replies"),
        ("identity", "0043_delegate_messages_operation"),
        ("intake", "0028_messages_operation"),
        ("scheduling", "0010_patient_service_booking"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    # One batch renders the three new models once; the SQL runs after them.
    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=[
                *_STATE,
                migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL),
            ],
            state_operations=list(_STATE),
        ),
    ]
