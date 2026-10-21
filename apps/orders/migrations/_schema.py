"""Frozen initial order/result model state, including encryption from first write."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import migrations, models
from django.db.models import deletion

from apps.tenancy.fields import EncryptedJSONField

PROTECT = deletion.PROTECT


def scope() -> list[tuple[str, models.Field[object, object]]]:
    """Create independent field instances for each migration-owned table."""
    return [
        (
            "id",
            models.UUIDField(
                primary_key=True, default=uuid.uuid4, editable=False, serialize=False
            ),
        ),
        ("created_at", models.DateTimeField(auto_now_add=True)),
        ("clinic", models.ForeignKey("identity.clinic", on_delete=PROTECT)),
        (
            "organization",
            models.ForeignKey("identity.organization", on_delete=deletion.CASCADE),
        ),
    ]


MODELS = [
    migrations.CreateModel(
        name="LabAccount",
        fields=[
            *scope(),
            (
                "principal",
                models.ForeignKey("identity.serviceprincipal", on_delete=PROTECT),
            ),
            ("provider", models.CharField(max_length=32, default="synthetic-lab-v1")),
            ("signing_key_digest", models.CharField(max_length=64)),
            ("active", models.BooleanField(default=True)),
        ],
    ),
    migrations.CreateModel(
        name="Order",
        fields=[
            *scope(),
            (
                "enrollment",
                models.ForeignKey("intake.patientclinicenrollment", on_delete=PROTECT),
            ),
            (
                "ordered_by",
                models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=PROTECT),
            ),
            ("command_id", models.UUIDField()),
            ("fingerprint", models.CharField(max_length=64)),
            (
                "lab_account",
                models.ForeignKey("orders.labaccount", null=True, on_delete=PROTECT),
            ),
            ("correlation", models.UUIDField(default=uuid.uuid4, unique=True)),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "command_id"), name="orders_place_command"
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="OrderItem",
        fields=[
            *scope(),
            (
                "order",
                models.ForeignKey(
                    "orders.order", on_delete=PROTECT, related_name="items"
                ),
            ),
            ("catalog_version", models.PositiveSmallIntegerField(default=1)),
            ("exam_code", models.CharField(max_length=32)),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("order", "exam_code"), name="orders_item_code"
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="OrderEvent",
        fields=[
            *scope(),
            (
                "order",
                models.ForeignKey(
                    "orders.order", on_delete=PROTECT, related_name="events"
                ),
            ),
            ("sequence", models.PositiveIntegerField()),
            ("state", models.CharField(max_length=16)),
            ("actor", models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=PROTECT)),
            ("command_id", models.UUIDField()),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("order", "sequence"), name="orders_event_sequence"
                ),
                models.UniqueConstraint(
                    fields=("order", "command_id"), name="orders_event_command"
                ),
                models.CheckConstraint(
                    condition=models.Q(
                        state__in=("draft", "placed", "collected", "in_process")
                    ),
                    name="orders_event_state",
                ),
            ]
        },
    ),
    migrations.CreateModel(name="Result", fields=scope()),
    migrations.CreateModel(
        name="ResultVersion",
        fields=[
            *scope(),
            (
                "result",
                models.ForeignKey(
                    "orders.result", on_delete=PROTECT, related_name="versions"
                ),
            ),
            ("number", models.PositiveIntegerField()),
            (
                "predecessor",
                models.OneToOneField(
                    "orders.resultversion",
                    null=True,
                    on_delete=PROTECT,
                    related_name="successor",
                ),
            ),
            ("event_id", models.UUIDField()),
            ("fingerprint", models.CharField(max_length=64)),
            ("proposed_order_id", models.UUIDField(null=True)),
            ("identity", EncryptedJSONField(purpose="orders.result.identity")),
            ("storage_key", models.CharField(max_length=64, unique=True)),
            ("sha256", models.CharField(max_length=64)),
            ("size_bytes", models.PositiveIntegerField()),
            ("content_type", models.CharField(max_length=32)),
            ("critical", models.BooleanField(default=False)),
            (
                "uploader",
                models.ForeignKey(
                    settings.AUTH_USER_MODEL, null=True, on_delete=PROTECT
                ),
            ),
            (
                "lab_account",
                models.ForeignKey("orders.labaccount", null=True, on_delete=PROTECT),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "event_id"), name="orders_result_event"
                ),
                models.UniqueConstraint(
                    fields=("result", "number"), name="orders_result_number"
                ),
                models.CheckConstraint(
                    condition=(
                        models.Q(uploader__isnull=False, lab_account__isnull=True)
                        | models.Q(uploader__isnull=True, lab_account__isnull=False)
                    ),
                    name="orders_source_identity",
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="ResultObservation",
        fields=[
            *scope(),
            (
                "version",
                models.ForeignKey(
                    "orders.resultversion",
                    on_delete=PROTECT,
                    related_name="observations",
                ),
            ),
            ("position", models.PositiveSmallIntegerField()),
            ("source", EncryptedJSONField(purpose="orders.observation.source")),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("version", "position"), name="orders_observation_position"
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="ResultMatch",
        fields=[
            *scope(),
            (
                "version",
                models.OneToOneField("orders.resultversion", on_delete=PROTECT),
            ),
            ("order", models.ForeignKey("orders.order", on_delete=PROTECT)),
            ("factors", models.JSONField()),
            (
                "operator",
                models.ForeignKey(
                    settings.AUTH_USER_MODEL, null=True, on_delete=PROTECT
                ),
            ),
            (
                "evidence_code",
                models.CharField(max_length=32, default="source_identity_verified"),
            ),
        ],
    ),
    migrations.CreateModel(
        name="ResultQuarantine",
        fields=[
            *scope(),
            (
                "version",
                models.OneToOneField("orders.resultversion", on_delete=PROTECT),
            ),
            ("reason", models.CharField(max_length=24)),
        ],
    ),
    migrations.CreateModel(
        name="ResultRejection",
        fields=[
            *scope(),
            (
                "version",
                models.OneToOneField("orders.resultversion", on_delete=PROTECT),
            ),
            (
                "operator",
                models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=PROTECT),
            ),
            ("evidence_code", models.CharField(max_length=32)),
        ],
    ),
    migrations.CreateModel(
        name="ResultAcknowledgment",
        fields=[
            *scope(),
            (
                "version",
                models.OneToOneField("orders.resultversion", on_delete=PROTECT),
            ),
            (
                "reviewer",
                models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=PROTECT),
            ),
            ("disposition", models.CharField(max_length=24)),
        ],
    ),
    migrations.CreateModel(
        name="ResultRelease",
        fields=[
            *scope(),
            ("version", models.ForeignKey("orders.resultversion", on_delete=PROTECT)),
            (
                "acknowledgment",
                models.ForeignKey("orders.resultacknowledgment", on_delete=PROTECT),
            ),
            (
                "released_by",
                models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=PROTECT),
            ),
            ("audience", models.CharField(max_length=8)),
            (
                "delegate_grant",
                models.ForeignKey(
                    "identity.patientdelegategrant", null=True, on_delete=PROTECT
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("version", "audience", "delegate_grant"),
                    nulls_distinct=False,
                    name="orders_release_audience",
                ),
                models.CheckConstraint(
                    condition=(
                        models.Q(audience="patient", delegate_grant__isnull=True)
                        | models.Q(audience="delegate", delegate_grant__isnull=False)
                    ),
                    name="orders_release_scope",
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="ResultWorkItem",
        fields=[
            *scope(),
            ("version", models.ForeignKey("orders.resultversion", on_delete=PROTECT)),
            ("task", models.OneToOneField("workflows.task", on_delete=PROTECT)),
            ("kind", models.CharField(max_length=16)),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("version", "kind"), name="orders_work_once"
                ),
            ]
        },
    ),
]
