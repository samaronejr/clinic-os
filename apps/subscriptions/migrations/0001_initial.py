"""Create the separate commercial schema with atomic FORCE RLS and source hooks."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.subscriptions.migrations import _metering_sql, _subscription_sql


def _install_foreign_keys(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Validate empty tables before the resolver backfills FORCE-RLS tenants."""
    del apps
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()


class Migration(migrations.Migration):
    """Install the complete additive subscription boundary."""

    initial = True

    dependencies: ClassVar = [
        ("identity", "0043_delegate_messages_operation"),
        ("ai", "0009_merge_care_coordination_front_desk"),
        ("comms", "0011_agent_replies"),
        ("ehr", "0016_later_pack_draft_guard"),
        ("teleconsult", "0007_consented_capture"),
    ]

    schema_operations: ClassVar = [
        migrations.CreateModel(
            name="Plan",
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
                ("code", models.CharField(max_length=32)),
                ("version", models.PositiveIntegerField(default=1)),
                ("provisional", models.BooleanField(default=True)),
                ("monthly_price_minor", models.PositiveBigIntegerField(null=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("code", "version"), name="subscriptions_plan_version"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "code__in",
                                (
                                    "core_operations",
                                    "intelligence_usage",
                                    "network_governance",
                                ),
                            ),
                            ("version__gte", 1),
                        ),
                        name="subscriptions_plan_contract",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="Subscription",
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
                ("state", models.CharField(default="trial", max_length=16)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("trial_ends_at", models.DateTimeField()),
                ("grace_ends_at", models.DateTimeField(null=True)),
                ("cancellation_at", models.DateTimeField(null=True)),
                ("created_at", models.DateTimeField()),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="subscriptions.plan",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Seat",
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
                ("created_at", models.DateTimeField()),
                (
                    "membership",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.userclinicrole",
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
                    "subscription",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="subscriptions.subscription",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="SubscriptionEvent",
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
                ("command_id", models.UUIDField()),
                ("action", models.CharField(max_length=32)),
                ("plan_code", models.CharField(default="", max_length=32)),
                ("from_state", models.CharField(max_length=16)),
                ("to_state", models.CharField(max_length=16)),
                ("revision", models.PositiveIntegerField()),
                ("occurred_at", models.DateTimeField()),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "subscription",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="subscriptions.subscription",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="UsageEvent",
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
                ("meter", models.CharField(max_length=32)),
                ("quantity", models.PositiveBigIntegerField()),
                ("occurred_at", models.DateTimeField()),
                ("source_ref", models.UUIDField()),
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
            name="UsageRollup",
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
                ("meter", models.CharField(max_length=32)),
                ("period", models.DateField()),
                ("quantity", models.PositiveBigIntegerField()),
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
            name="Entitlement",
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
                ("meter", models.CharField(max_length=32)),
                ("enabled", models.BooleanField(default=True)),
                ("quota", models.PositiveBigIntegerField()),
                (
                    "plan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="subscriptions.plan",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("plan", "meter"), name="subscriptions_entitlement_key"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "meter__in",
                                (
                                    "ai_tokens_in",
                                    "ai_tokens_out",
                                    "ai_tokens_cached",
                                    "ai_audio_seconds",
                                    "ai_cost_micros",
                                    "video_seconds",
                                    "messages",
                                    "storage_bytes",
                                    "seats",
                                ),
                            )
                        ),
                        name="subscriptions_meter_contract",
                    ),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.UniqueConstraint(
                fields=("organization",), name="subscriptions_one_per_org"
            ),
        ),
        migrations.AddConstraint(
            model_name="subscription",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "state__in",
                        (
                            "trial",
                            "active",
                            "past_due",
                            "grace",
                            "restricted",
                            "cancelled",
                            "offboarded",
                        ),
                    ),
                    ("revision__gte", 1),
                ),
                name="subscriptions_state_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="subscriptionevent",
            constraint=models.UniqueConstraint(
                fields=("organization", "command_id"), name="subscriptions_event_key"
            ),
        ),
        migrations.AddConstraint(
            model_name="subscriptionevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "action__in",
                        (
                            "trial_started",
                            "plan_changed",
                            "cancel",
                            "paid",
                            "payment_failed",
                            "advance",
                            "offboard",
                        ),
                    ),
                    (
                        "to_state__in",
                        (
                            "trial",
                            "active",
                            "past_due",
                            "grace",
                            "restricted",
                            "cancelled",
                            "offboarded",
                        ),
                    ),
                    (
                        "from_state__in",
                        (
                            "",
                            "trial",
                            "active",
                            "past_due",
                            "grace",
                            "restricted",
                            "cancelled",
                            "offboarded",
                        ),
                    ),
                ),
                name="subscriptions_event_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="usageevent",
            constraint=models.UniqueConstraint(
                fields=("organization", "meter", "source_ref"),
                name="subscriptions_usage_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="usageevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "meter__in",
                        (
                            "ai_tokens_in",
                            "ai_tokens_out",
                            "ai_tokens_cached",
                            "ai_audio_seconds",
                            "ai_cost_micros",
                            "video_seconds",
                            "messages",
                            "storage_bytes",
                            "seats",
                        ),
                    ),
                    models.Q(("meter", "seats"), _negated=True),
                ),
                name="subscriptions_usage_meter",
            ),
        ),
        migrations.AddConstraint(
            model_name="usagerollup",
            constraint=models.UniqueConstraint(
                fields=("organization", "meter", "period"),
                name="subscriptions_rollup_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="usagerollup",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "meter__in",
                        (
                            "ai_tokens_in",
                            "ai_tokens_out",
                            "ai_tokens_cached",
                            "ai_audio_seconds",
                            "ai_cost_micros",
                            "video_seconds",
                            "messages",
                            "storage_bytes",
                            "seats",
                        ),
                    ),
                    models.Q(("meter", "seats"), _negated=True),
                ),
                name="subscriptions_rollup_meter",
            ),
        ),
    ]

    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=schema_operations, state_operations=schema_operations
        ),
        migrations.RunPython(_install_foreign_keys, migrations.RunPython.noop),
        migrations.RunSQL(_subscription_sql.SQL, _subscription_sql.REVERSE),
        migrations.RunSQL(_metering_sql.SQL, _metering_sql.REVERSE),
    ]
