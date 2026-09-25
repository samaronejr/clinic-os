"""Add v1 staff scope without rewriting existing roles or protected data."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.fields

from ._permission_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Create bytea columns and install their ACLs in the same DDL transaction."""

    dependencies: ClassVar = [
        ("identity", "0012_queue_quotas"),
        ("intake", "0011_protected_fields"),
    ]

    operations: ClassVar = [
        migrations.AlterField(
            model_name="userclinicrole",
            name="role",
            field=models.CharField(
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
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="CareTeamMembership",
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
                    "role",
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
                        max_length=20,
                    ),
                ),
                ("valid_from", models.DateTimeField()),
                ("valid_to", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
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
                    "patient_enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientclinicenrollment",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("role__in", ("physician", "nurse", "allied_professional"))
                        ),
                        name="identity_careteam_clinical_role",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("valid_to__isnull", True),
                            ("valid_to__gt", models.F("valid_from")),
                            _connector="OR",
                        ),
                        name="identity_careteam_window",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ProfessionalRegistration",
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
                    "role",
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
                        max_length=20,
                    ),
                ),
                ("council", models.CharField(max_length=16)),
                (
                    "number",
                    apps.tenancy.fields.EncryptedTextField(
                        purpose="identity.registration.number"
                    ),
                ),
                ("jurisdiction", models.CharField(max_length=2)),
                (
                    "specialty",
                    apps.tenancy.fields.EncryptedTextField(
                        null=True, purpose="identity.registration.specialty"
                    ),
                ),
                ("synthetic", models.BooleanField(default=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("regular", "Regular"),
                            ("expired", "Expired"),
                            ("revoked", "Revoked"),
                            ("suspended", "Suspended"),
                            ("unknown", "Unknown"),
                            ("unavailable", "Unavailable"),
                        ],
                        default="unknown",
                        max_length=16,
                    ),
                ),
                ("valid_from", models.DateTimeField()),
                ("valid_to", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
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
                    "physician_profile",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.physicianprofile",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("synthetic", True),
                            (
                                "status__in",
                                [
                                    "regular",
                                    "expired",
                                    "revoked",
                                    "suspended",
                                    "unknown",
                                    "unavailable",
                                ],
                            ),
                            ("council__regex", "^[A-Z][A-Z0-9]{1,15}$"),
                            ("jurisdiction__regex", "^[A-Z]{2}$"),
                            ("valid_to__gt", models.F("valid_from")),
                        ),
                        name="identity_registration_synthetic",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("council", "CRM"), ("role", "physician")),
                            models.Q(("council", "COREN"), ("role", "nurse")),
                            models.Q(
                                ("role", "allied_professional"),
                                models.Q(
                                    ("council__in", ("CRM", "COREN")), _negated=True
                                ),
                            ),
                            _connector="OR",
                        ),
                        name="identity_registration_profession",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="RoleGrant",
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
                    "role",
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
                        max_length=20,
                    ),
                ),
                ("permission", models.CharField(max_length=64)),
                ("bundle_version", models.PositiveSmallIntegerField(default=1)),
                ("effect", models.CharField(default="remove", max_length=6)),
                ("valid_from", models.DateTimeField()),
                ("valid_to", models.DateTimeField(blank=True, null=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
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
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("bundle_version", 1), ("effect", "remove")),
                        name="identity_rolegrant_remove_only",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("valid_to__isnull", True),
                            ("valid_to__gt", models.F("valid_from")),
                            _connector="OR",
                        ),
                        name="identity_rolegrant_window",
                    ),
                ],
            },
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
