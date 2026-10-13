"""Add protected staff lifecycle state and retain revoked membership history."""

import uuid
from collections.abc import Sequence
from typing import ClassVar

import django.db.models.deletion
import django.db.models.manager
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import apps.tenancy.fields
from apps.identity.migrations._lifecycle_sql import FORWARD_SQL


class Migration(migrations.Migration):
    """Keep protected state and authority history irreversibly."""

    atomic = False

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("comms", "0006_action_operations"),
        ("identity", "0018_signup"),
        ("intake", "0013_patient_demographics"),
        ("otp_static", "0003_add_timestamps"),
        ("otp_totp", "0003_add_timestamps"),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.CreateModel(
            name="AuthenticatorReset",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("notified_at", models.DateTimeField(blank=True, null=True)),
                ("available_at", models.DateTimeField(blank=True, null=True)),
                ("executed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="AuthenticatorRetirement",
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
                ("retired_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="OnboardingProgress",
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
                ("step_key", models.CharField(max_length=80)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("reviewed_fingerprint", models.BinaryField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name="RecoveryBatch",
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
                ("issued_at", models.DateTimeField(auto_now_add=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="RecoveryCode",
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
                ("code_hash", models.BinaryField(unique=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="SessionRecord",
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
                ("session_key_hash", models.BinaryField(unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen", models.DateTimeField(auto_now_add=True)),
                ("ip_network", models.CharField(blank=True, max_length=48)),
                (
                    "user_agent_family",
                    models.CharField(
                        choices=[
                            ("chromium", "Chromium"),
                            ("firefox", "Firefox"),
                            ("safari", "Safari"),
                            ("other", "Other browser"),
                        ],
                        default="other",
                        max_length=16,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name="StaffInvitation",
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
                ("email_hash", models.BinaryField()),
                (
                    "email",
                    apps.tenancy.fields.EncryptedTextField(
                        purpose="identity.staff.invitation.email"
                    ),
                ),
                (
                    "bundle",
                    models.CharField(
                        choices=[
                            ("owner", "Owner"),
                            ("physician", "Physician"),
                            ("receptionist", "Receptionist"),
                            ("clinic_admin", "Clinic admin"),
                            ("nurse", "Nurse"),
                            ("allied_professional", "Allied professional"),
                            ("scheduler", "Scheduler"),
                            ("clinic_manager", "Clinic manager"),
                            ("finance", "Finance"),
                            ("org_admin", "Organization admin"),
                        ],
                        max_length=32,
                    ),
                ),
                ("token_hash", models.BinaryField(unique=True)),
                ("token_nonce", models.UUIDField(default=uuid.uuid4, editable=False)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("accepted", "Accepted"),
                            ("expired", "Expired"),
                            ("revoked", "Revoked"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "desired_username",
                    apps.tenancy.fields.EncryptedTextField(
                        blank=True,
                        null=True,
                        purpose="identity.staff.invitation.username",
                    ),
                ),
                ("password_set_at", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.AlterModelOptions(
            name="userclinicrole",
            options={
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
            },
        ),
        migrations.AlterModelManagers(
            name="userclinicrole",
            managers=[
                ("objects", django.db.models.manager.Manager()),
                ("all_objects", django.db.models.manager.Manager()),
            ],
        ),
        migrations.RemoveConstraint(
            model_name="userclinicrole",
            name="identity_userclinicrole_assignment_uniq",
        ),
        migrations.AddField(
            model_name="userclinicrole",
            name="revoked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="userclinicrole",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AlterField(
            model_name="userclinicrole",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.organization"
            ),
        ),
        migrations.AlterField(
            model_name="userclinicrole",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddConstraint(
            model_name="userclinicrole",
            constraint=models.UniqueConstraint(
                condition=models.Q(("revoked_at__isnull", True)),
                fields=("organization", "clinic", "user", "role"),
                name="identity_userclinicrole_assignment_uniq",
            ),
        ),
        migrations.AddField(
            model_name="authenticatorreset",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="authenticatorreset",
            name="notification_operation",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="comms.integrationoperation",
            ),
        ),
        migrations.AddField(
            model_name="authenticatorreset",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="authenticatorreset",
            name="requested_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="requested_authenticator_resets",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="authenticatorreset",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="authenticator_resets",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="authenticatorretirement",
            name="device",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="lifecycle_retirement",
                to="otp_totp.totpdevice",
            ),
        ),
        migrations.AddField(
            model_name="authenticatorretirement",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="onboardingprogress",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="onboardingprogress",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="recoverybatch",
            name="device",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="otp_totp.totpdevice"
            ),
        ),
        migrations.AddField(
            model_name="recoverybatch",
            name="static_device",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                to="otp_static.staticdevice",
            ),
        ),
        migrations.AddField(
            model_name="recoverybatch",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="recoverycode",
            name="batch",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.recoverybatch"
            ),
        ),
        migrations.AddField(
            model_name="sessionrecord",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="sessionrecord",
            name="verification_device",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="otp_totp.totpdevice",
            ),
        ),
        migrations.AddField(
            model_name="staffinvitation",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="staffinvitation",
            name="device",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="otp_totp.totpdevice",
            ),
        ),
        migrations.AddField(
            model_name="staffinvitation",
            name="invited_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="issued_staff_invitations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="staffinvitation",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="staffinvitation",
            name="pending_user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="staff_invitations",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="onboardingprogress",
            constraint=models.UniqueConstraint(
                fields=("clinic", "step_key"),
                name="identity_onboarding_clinic_step_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="staffinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("state__in", ("pending", "accepted", "expired", "revoked"))
                ),
                name="identity_invitation_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="staffinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(("expires_at__gt", models.F("created_at"))),
                name="identity_invitation_expiry",
            ),
        ),
        migrations.AddConstraint(
            model_name="staffinvitation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("accepted_at__isnull", False),
                        ("device__isnull", False),
                        ("password_set_at__isnull", False),
                        ("state", "accepted"),
                    ),
                    models.Q(
                        models.Q(("state", "accepted"), _negated=True),
                        ("accepted_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="identity_invitation_acceptance",
            ),
        ),
        migrations.RunSQL(FORWARD_SQL),
    ]
