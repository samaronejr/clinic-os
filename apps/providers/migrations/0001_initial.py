"""Create the provider capability lifecycle registry and seed set v2."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.operations.base import Operation

from apps.providers.migrations._lifecycle_sql import (
    PROVIDER_TABLES,
    REVERSE_RUNTIME_ACL_SQL,
    REVERSE_TRANSITION_SQL,
    TRANSITION_SQL,
)
from apps.providers.migrations._seed_v2 import seed_v2_capabilities
from apps.tenancy.migrations_support import revoke_default_dml

RUNTIME_ACL_SQL: str = "".join(
    revoke_default_dml(table, {"SELECT"}) for table in PROVIDER_TABLES
)


def _seed(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Insert the 2026-09-24-v2 capability registry."""
    del schema_editor
    seed_v2_capabilities(apps)


class Migration(migrations.Migration):
    """Install the owner-managed provider lifecycle tables."""

    initial = True

    dependencies: ClassVar[list[tuple[str, str]]] = []
    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="CapabilityApproval",
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
                ("approver_name", models.CharField(max_length=255)),
                ("approver_role", models.CharField(max_length=255)),
                ("evidence_uri", models.TextField(blank=True)),
                ("decided_at", models.DateTimeField()),
            ],
            options={
                "db_table": "providers_capabilityapproval",
            },
        ),
        migrations.CreateModel(
            name="CapabilityVersion",
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
                ("provider", models.CharField(max_length=255)),
                ("account", models.CharField(blank=True, max_length=255)),
                ("environment", models.CharField(blank=True, max_length=32)),
                ("api_version", models.CharField(blank=True, max_length=128)),
                ("region", models.CharField(blank=True, max_length=64)),
                ("retention_terms", models.TextField(blank=True)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("researched", "Researched"),
                            ("selected_in_plan", "Selected in plan"),
                            ("approved_to_test", "Approved to test"),
                            ("sandbox", "Sandbox"),
                            ("production_authorized", "Production authorized"),
                            ("activated", "Activated"),
                            ("degraded", "Degraded"),
                            ("revoked", "Revoked"),
                        ],
                        default="researched",
                        max_length=24,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "approval",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="providers.capabilityapproval",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ProviderCapability",
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
                ("key", models.CharField(max_length=64)),
                ("clinic_id", models.UUIDField(blank=True, null=True)),
                ("record_ref", models.CharField(blank=True, max_length=128)),
                ("description", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "current_version",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="providers.capabilityversion",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="HealthEvent",
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
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("degraded", "Degraded"),
                            ("revoked", "Revoked"),
                            ("note", "Note"),
                        ],
                        max_length=16,
                    ),
                ),
                ("detail", models.TextField(blank=True)),
                ("recorded_at", models.DateTimeField()),
                (
                    "approval",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="providers.capabilityapproval",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="health_events",
                        to="providers.capabilityversion",
                    ),
                ),
                (
                    "capability",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="health_events",
                        to="providers.providercapability",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="capabilityversion",
            name="capability",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="versions",
                to="providers.providercapability",
            ),
        ),
        migrations.AddField(
            model_name="capabilityapproval",
            name="capability",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="approvals",
                to="providers.providercapability",
            ),
        ),
        migrations.CreateModel(
            name="ActivationRecord",
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
                ("activated_at", models.DateTimeField()),
                (
                    "approval",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="providers.capabilityapproval",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="activation_records",
                        to="providers.capabilityversion",
                    ),
                ),
                (
                    "capability",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="activation_records",
                        to="providers.providercapability",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="providercapability",
            constraint=models.UniqueConstraint(
                fields=("key", "clinic_id"),
                name="providers_capability_key_scope_uniq",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="providercapability",
            constraint=models.CheckConstraint(
                condition=models.Q(("key__regex", "^[a-z][a-z0-9_]{0,63}$")),
                name="providers_capability_key_format",
            ),
        ),
        migrations.AddConstraint(
            model_name="capabilityversion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "state__in",
                        (
                            "researched",
                            "selected_in_plan",
                            "approved_to_test",
                            "sandbox",
                            "production_authorized",
                            "activated",
                            "degraded",
                            "revoked",
                        ),
                    )
                ),
                name="providers_capabilityversion_state_vocab",
            ),
        ),
        migrations.AddConstraint(
            model_name="capabilityversion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("approval__isnull", True),
                        ("state__in", ("researched", "selected_in_plan")),
                    ),
                    models.Q(
                        ("approval__isnull", False),
                        (
                            "state__in",
                            (
                                "approved_to_test",
                                "sandbox",
                                "production_authorized",
                                "activated",
                                "degraded",
                                "revoked",
                            ),
                        ),
                    ),
                    _connector="OR",
                ),
                name="providers_capabilityversion_approval_pairing",
            ),
        ),
        migrations.AddConstraint(
            model_name="capabilityversion",
            constraint=models.UniqueConstraint(
                fields=(
                    "capability",
                    "provider",
                    "account",
                    "environment",
                    "api_version",
                    "region",
                ),
                name="providers_capabilityversion_identity_uniq",
            ),
        ),
        migrations.RunSQL(TRANSITION_SQL, reverse_sql=REVERSE_TRANSITION_SQL),
        migrations.RunSQL(RUNTIME_ACL_SQL, reverse_sql=REVERSE_RUNTIME_ACL_SQL),
        migrations.RunPython(_seed, migrations.RunPython.noop),
    ]
