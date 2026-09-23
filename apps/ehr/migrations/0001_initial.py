"""Create retained encounter, template and clinical document identities."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Introduce the task-21 clinical record schema."""

    initial = True

    dependencies: ClassVar = [
        ("identity", "0005_clinic_timezone"),
        (
            "intake",
            "0006_remove_patientchannelpreference_intake_preference_purpose_check_and_more",
        ),
        ("scheduling", "0004_waitlistentry_waitlistoffer_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="Encounter",
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
                    "state",
                    models.CharField(
                        choices=[("open", "Aberto"), ("closed", "Encerrado")],
                        default="open",
                        max_length=16,
                    ),
                ),
                ("revision", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "appointment",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scheduling.appointment",
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
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
                    ),
                ),
                (
                    "physician",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ClinicalDocument",
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
                ("kind", models.CharField(default="soap", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "encounter",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="EncounterIntakeReference",
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
                (
                    "encounter",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="ehr.encounter"
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
                    "submission",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.questionnaireevent",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="SpecialtyTemplate",
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
                ("key", models.SlugField(max_length=64)),
                ("version", models.PositiveIntegerField()),
                ("title", models.CharField(max_length=160)),
                ("prompts", models.JSONField(default=dict)),
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
            ],
        ),
        migrations.CreateModel(
            name="ClinicalDocumentVersion",
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
                ("version", models.PositiveIntegerField(default=1)),
                ("revision", models.PositiveIntegerField(default=1)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("draft", "Rascunho"),
                            ("finalized", "Finalizado"),
                            ("superseded", "Substituído"),
                            ("discarded", "Descartado"),
                        ],
                        default="draft",
                        max_length=16,
                    ),
                ),
                ("subjective", models.TextField(blank=True, default="")),
                ("objective", models.TextField(blank=True, default="")),
                ("assessment", models.TextField(blank=True, default="")),
                ("plan", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.clinicaldocument",
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
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.specialtytemplate",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="encounter",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("closed_at__isnull", True), ("state", "open")),
                    models.Q(("closed_at__isnull", False), ("state", "closed")),
                    _connector="OR",
                ),
                name="ehr_encounter_state_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicaldocument",
            constraint=models.UniqueConstraint(
                fields=("encounter", "kind"), name="ehr_document_kind_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="encounterintakereference",
            constraint=models.UniqueConstraint(
                fields=("encounter", "submission"), name="ehr_intake_reference_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="specialtytemplate",
            constraint=models.UniqueConstraint(
                fields=("clinic", "key", "version"), name="ehr_template_version_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="specialtytemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)),
                name="ehr_template_version_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicaldocumentversion",
            constraint=models.UniqueConstraint(
                fields=("document", "version"), name="ehr_document_version_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicaldocumentversion",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "draft")),
                fields=("document",),
                name="ehr_document_one_draft",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicaldocumentversion",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "finalized")),
                fields=("document",),
                name="ehr_document_one_current",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicaldocumentversion",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("state__in", ["draft", "finalized", "superseded", "discarded"])
                ),
                name="ehr_version_state_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="clinicaldocumentversion",
            constraint=models.CheckConstraint(
                condition=models.Q(("revision__gte", 1), ("version__gte", 1)),
                name="ehr_version_positive",
            ),
        ),
    ]
