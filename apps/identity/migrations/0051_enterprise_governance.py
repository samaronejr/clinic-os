"""Add encrypted directory and expiring governance history with exact posture."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from apps.identity.migrations._enterprise_sql import REVERSE_SQL, SQL
from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField


def _scope() -> list[tuple[str, models.Field[Any, Any]]]:
    return [
        (
            "id",
            models.UUIDField(
                primary_key=True, default=uuid.uuid4, editable=False, serialize=False
            ),
        ),
        (
            "organization",
            models.ForeignKey(
                to="identity.organization", on_delete=django.db.models.deletion.CASCADE
            ),
        ),
        (
            "clinic",
            models.ForeignKey(
                to="identity.clinic", on_delete=django.db.models.deletion.PROTECT
            ),
        ),
    ]


def _user(
    *, nullable: bool = False, related: str | None = None
) -> models.ForeignKey[Any, Any]:
    return models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        on_delete=django.db.models.deletion.PROTECT,
        null=nullable,
        related_name=related,
    )


def _link(
    model: str, *, nullable: bool = False, related: str | None = None
) -> models.ForeignKey[Any, Any]:
    return models.ForeignKey(
        to=model,
        on_delete=django.db.models.deletion.PROTECT,
        null=nullable,
        related_name=related,
    )


class Migration(migrations.Migration):
    """Ciphertext ships from creation; populated authority history rollback is restore.

    The policy fence narrows every clinic_app policy that exists when it runs,
    so this migration follows the leaf of every app. An unused install reverses
    exactly (tests/identity/test_enterprise_migration.py); any enterprise row
    makes the reverse refuse before it changes anything.
    """

    dependencies: ClassVar = [
        ("identity", "0050_totp_secret_boundary"),
        ("ai", "0012_approval_attestation"),
        ("analytics", "0001_initial"),
        ("audit", "0005_clinic_metadata_v2"),
        ("billing", "0014_merge_sandbox_psp_fiscal_documents"),
        ("comms", "0011_agent_replies"),
        ("consent", "0005_delegate_purposes"),
        ("crm", "0001_initial"),
        ("ehr", "0017_split_longitudinal_guard"),
        ("insurance", "0002_distinct_reverse_accessors"),
        ("intake", "0029_membership_payer"),
        ("inventory", "0001_inventory"),
        ("orders", "0005_patient_downloads"),
        ("prescription", "0015_sncr_issuance"),
        ("providers", "0001_initial"),
        ("retention", "0005_transient_purge"),
        ("scheduling", "0011_insurer_membership"),
        ("scribe", "0005_note_drafts"),
        ("subscriptions", "0001_initial"),
        ("teleconsult", "0007_consented_capture"),
        ("tenancy", "0007_blind_indexes"),
        ("workflows", "0009_metrics_refresh"),
    ]
    operations: ClassVar = [
        migrations.CreateModel(
            name="SsoConnection",
            fields=[
                *_scope(),
                (
                    "configuration",
                    EncryptedJSONField(purpose="identity.sso.configuration"),
                ),
                (
                    "provider_key",
                    models.CharField(max_length=64, default="enterprise_identity"),
                ),
                ("created_by", _user()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("revoked_at", models.DateTimeField(null=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization",),
                        condition=models.Q(revoked_at__isnull=True),
                        name="identity_sso_one_current",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="ScimToken",
            fields=[
                *_scope(),
                ("connection", _link("identity.ssoconnection", related="tokens")),
                ("secret_hash", models.BinaryField(unique=True)),
                ("issued_by", _user()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(null=True)),
                ("window_start", models.DateTimeField()),
                ("window_count", models.PositiveIntegerField(default=0)),
            ],
        ),
        migrations.CreateModel(
            name="ScimResource",
            fields=[
                *_scope(),
                ("connection", _link("identity.ssoconnection")),
                ("kind", models.CharField(max_length=6)),
                ("user", _user(nullable=True, related="+")),
                ("external_hash", models.BinaryField(null=True)),
                ("search_hash", models.BinaryField(db_index=True)),
                ("attributes", EncryptedJSONField(purpose="identity.scim.attributes")),
                ("active", models.BooleanField(default=True)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_at", models.DateTimeField(null=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(kind="Users", user__isnull=False)
                        | models.Q(kind="Groups", user__isnull=True),
                        name="identity_scim_resource_shape",
                    ),
                    models.UniqueConstraint(
                        fields=("connection", "kind", "external_hash"),
                        name="identity_scim_external_unique",
                    ),
                    models.UniqueConstraint(
                        fields=("connection", "search_hash"),
                        condition=models.Q(kind="Users", deleted_at__isnull=True),
                        name="identity_scim_username_current",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="ScimRoleMapping",
            fields=[
                *_scope(),
                ("group", _link("identity.scimresource")),
                ("unit", _link("identity.clinic", related="+")),
                ("bundle", models.CharField(max_length=20)),
                ("approved_by", _user()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("revoked_at", models.DateTimeField(null=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("group", "unit", "bundle"),
                        condition=models.Q(revoked_at__isnull=True),
                        name="identity_scim_mapping_current",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="ScimMembership",
            fields=[
                *_scope(),
                ("group", _link("identity.scimresource", related="+")),
                ("member", _link("identity.scimresource", related="+")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("revoked_at", models.DateTimeField(null=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("group", "member"),
                        condition=models.Q(revoked_at__isnull=True),
                        name="identity_scim_member_current",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="ScimRoleBinding",
            fields=[
                *_scope(),
                ("resource", _link("identity.scimresource")),
                (
                    "membership",
                    models.ForeignKey(
                        to="identity.userclinicrole",
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
                ("manages_membership", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("revoked_at", models.DateTimeField(null=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("membership",),
                        condition=models.Q(revoked_at__isnull=True),
                        name="identity_scim_binding_current",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="BreakGlassGrant",
            fields=[
                *_scope(),
                ("clinician", _user()),
                ("enrollment", _link("intake.patientclinicenrollment")),
                ("encounter", _link("ehr.encounter", nullable=True)),
                ("reason", EncryptedTextField(purpose="identity.break_glass.reason")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(null=True)),
                ("reviewed_at", models.DateTimeField(null=True)),
                ("reviewed_by", _user(nullable=True, related="+")),
                (
                    "review_task",
                    models.OneToOneField(
                        to="workflows.task",
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(expires_at__gt=models.F("created_at"))
                        & models.Q(
                            expires_at__lte=models.F("created_at") + timedelta(hours=4)
                        ),
                        name="identity_break_glass_four_hours",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="SupportAccessGrant",
            fields=[
                *_scope(),
                ("support_user", _user(related="+")),
                ("approved_by", _user(related="+")),
                ("purpose", EncryptedTextField(purpose="identity.support.purpose")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(null=True)),
                ("reviewed_at", models.DateTimeField(null=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(expires_at__gt=models.F("created_at")),
                        name="identity_support_bounded",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="GovernanceNotice",
            fields=[
                *_scope(),
                ("break_glass", _link("identity.breakglassgrant")),
                ("audience", models.CharField(max_length=8)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(audience__in=("manager", "patient")),
                        name="identity_governance_audience",
                    ),
                    models.UniqueConstraint(
                        fields=("break_glass", "audience"),
                        name="identity_governance_notice_once",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="UnitGovernanceChange",
            fields=[
                *_scope(),
                ("kind", models.CharField(max_length=24)),
                (
                    "references",
                    EncryptedJSONField(purpose="identity.unit_governance.references"),
                ),
                ("created_by", _user()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("target_user", _user(nullable=True, related="+")),
                ("access_denied", models.BooleanField(null=True)),
                ("policy_version", models.PositiveIntegerField(null=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            kind__in=(
                                "unit_membership",
                                "template_propagation",
                                "credential_rotation",
                                "recovery_exercise",
                            )
                        ),
                        name="identity_unit_governance_kind",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            kind="unit_membership",
                            target_user__isnull=False,
                            access_denied__isnull=False,
                            policy_version__gte=1,
                        )
                        | (
                            ~models.Q(kind="unit_membership")
                            & models.Q(
                                target_user__isnull=True,
                                access_denied__isnull=True,
                                policy_version__isnull=True,
                            )
                        ),
                        name="identity_unit_policy_shape",
                    ),
                    models.UniqueConstraint(
                        fields=("clinic", "target_user", "policy_version"),
                        condition=models.Q(kind="unit_membership"),
                        name="identity_unit_policy_version",
                    ),
                ]
            },
        ),
        migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL),
    ]
