"""Create retained consent texts, acceptances and revocations."""

import uuid
from collections.abc import Sequence
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    """Store versioned consent without destructive lifecycle fields."""

    initial = True

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0005_clinic_timezone"),
        ("intake", "0007_records_operation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.CreateModel(
            name="ConsentAcceptance",
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
                    "authority",
                    models.CharField(default="patient_explicit_action", max_length=32),
                ),
                ("accepted_at", models.DateTimeField()),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientclinicenrollment",
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
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
                    ),
                ),
                (
                    "patient_session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientsession",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ConsentRevocation",
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
                ("revoked_at", models.DateTimeField()),
                (
                    "acceptance",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="revocation",
                        to="consent.consentacceptance",
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
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "patient_session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientsession",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="ConsentText",
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
                    "purpose",
                    models.CharField(
                        choices=[("teleconsultation", "Teleconsulta")], max_length=32
                    ),
                ),
                ("version", models.PositiveIntegerField()),
                ("language", models.CharField(default="pt-BR", max_length=16)),
                ("text", models.TextField()),
                ("digest", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
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
                    "published_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="consentacceptance",
            name="text",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="consent.consenttext"
            ),
        ),
        migrations.AddConstraint(
            model_name="consenttext",
            constraint=models.UniqueConstraint(
                fields=("clinic", "purpose", "version"),
                name="consent_text_version_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="consenttext",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("language", "pt-BR"),
                    ("purpose", "teleconsultation"),
                    ("version__gte", 1),
                ),
                name="consent_text_scope_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="consentacceptance",
            constraint=models.UniqueConstraint(
                fields=("enrollment", "text"), name="consent_acceptance_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="consentacceptance",
            constraint=models.CheckConstraint(
                condition=models.Q(("authority", "patient_explicit_action")),
                name="consent_authority_check",
            ),
        ),
    ]
