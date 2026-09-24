"""Separate professional profiles and registry evidence from staff assignments."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Create task-31 professional identity and normalized evidence storage."""

    dependencies: ClassVar = [
        ("ehr", "0008_finalization_policy"),
        ("identity", "0005_clinic_timezone"),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="PhysicianProfile",
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
                ("jurisdiction", models.CharField(max_length=2)),
                ("registration_number", models.CharField(max_length=64)),
                ("signing_subject", models.CharField(max_length=255)),
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
                ("expires_at", models.DateTimeField(null=True)),
                ("last_checked_at", models.DateTimeField(null=True)),
                ("recheck_at", models.DateTimeField(null=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.organization",
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
        ),
        migrations.CreateModel(
            name="PhysicianEvidence",
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
                ("attempted_at", models.DateTimeField(auto_now_add=True)),
                ("provider", models.CharField(max_length=64)),
                ("reference", models.CharField(blank=True, max_length=255)),
                ("synthetic", models.BooleanField()),
                ("jurisdiction", models.CharField(blank=True, max_length=2)),
                ("registration_number", models.CharField(blank=True, max_length=64)),
                ("signing_subject", models.CharField(blank=True, max_length=255)),
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
                        max_length=16,
                    ),
                ),
                ("checked_at", models.DateTimeField(null=True)),
                ("expires_at", models.DateTimeField(null=True)),
                ("recheck_at", models.DateTimeField(null=True)),
                ("reason_code", models.CharField(max_length=64)),
                (
                    "encounter",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.physicianprofile",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="physicianprofile",
            constraint=models.UniqueConstraint(
                fields=("organization", "user", "jurisdiction"),
                name="identity_physician_jurisdiction_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="physicianprofile",
            constraint=models.UniqueConstraint(
                fields=("organization", "jurisdiction", "registration_number"),
                name="identity_physician_registration_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="physicianprofile",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("jurisdiction__regex", "^[A-Z]{2}$"),
                    models.Q(("registration_number", ""), _negated=True),
                    models.Q(("signing_subject", ""), _negated=True),
                ),
                name="identity_physician_identity_nonblank",
            ),
        ),
    ]
