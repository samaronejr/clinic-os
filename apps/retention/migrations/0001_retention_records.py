"""Create retention policy, hold, release and export receipt rows."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Introduce the task-25 retention schema; disposal stays unimplemented."""

    initial = True

    dependencies: ClassVar = [
        ("ehr", "0008_finalization_policy"),
        ("identity", "0005_clinic_timezone"),
        ("intake", "0007_records_operation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="LegalHold",
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
                ("record_class", models.CharField(max_length=64)),
                ("record_id", models.UUIDField()),
                ("authority", models.CharField(max_length=255)),
                ("reason", models.CharField(max_length=255)),
                (
                    "release_authority",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "release_reason",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("released_at", models.DateTimeField(blank=True, null=True)),
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
                    "placed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legal_holds_placed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "released_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legal_holds_released",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RecordExport",
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
                        choices=[("staff", "Equipe"), ("patient", "Paciente")],
                        max_length=16,
                    ),
                ),
                ("record_count", models.PositiveIntegerField()),
                ("manifest", models.JSONField(default=dict)),
                ("manifest_digest", models.CharField(max_length=64)),
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
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patient",
                    ),
                ),
                (
                    "patient_session",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientsession",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RecordRelease",
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
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
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
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patient",
                    ),
                ),
                (
                    "released_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="record_releases_made",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "revoked_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="record_releases_revoked",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.clinicaldocumentversion",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RetentionPolicy",
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
                ("record_class", models.CharField(max_length=64)),
                ("version", models.PositiveIntegerField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("proposed", "Proposta"),
                            ("approved", "Aprovada"),
                            ("retired", "Retirada"),
                        ],
                        default="proposed",
                        max_length=16,
                    ),
                ),
                ("retention_days", models.PositiveIntegerField(blank=True, null=True)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("retired_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="retention_policies_approved",
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
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "proposed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="retention_policies_proposed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "retired_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="retention_policies_retired",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="retentionpolicy",
            constraint=models.UniqueConstraint(
                fields=("clinic", "record_class", "version"),
                name="retention_policy_version_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="retentionpolicy",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "approved")),
                fields=("clinic", "record_class"),
                name="retention_policy_one_approved",
            ),
        ),
        migrations.AddConstraint(
            model_name="retentionpolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "record_class__in",
                        [
                            "ehr.encounter",
                            "ehr.document_version",
                            "ehr.clinical_attachment",
                            "ehr.history_assessment",
                            "ehr.problem",
                            "ehr.allergy",
                        ],
                    )
                ),
                name="retention_policy_class_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="retentionpolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(("state__in", ["proposed", "approved", "retired"])),
                name="retention_policy_state_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="retentionpolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)),
                name="retention_policy_version_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="retentionpolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("approved_at__isnull", True),
                        ("approved_by__isnull", True),
                        ("retired_at__isnull", True),
                        ("retired_by__isnull", True),
                        ("state", "proposed"),
                    ),
                    models.Q(
                        ("approved_at__isnull", False),
                        ("approved_by__isnull", False),
                        ("retired_at__isnull", True),
                        ("retired_by__isnull", True),
                        ("state", "approved"),
                    ),
                    models.Q(
                        ("retired_at__isnull", False),
                        ("retired_by__isnull", False),
                        ("state", "retired"),
                    ),
                    _connector="OR",
                ),
                name="retention_policy_transition_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="legalhold",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "record_class__in",
                        [
                            "ehr.encounter",
                            "ehr.document_version",
                            "ehr.clinical_attachment",
                            "ehr.history_assessment",
                            "ehr.problem",
                            "ehr.allergy",
                        ],
                    )
                ),
                name="retention_hold_class_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="legalhold",
            constraint=models.CheckConstraint(
                condition=models.Q(("authority__regex", "\\S"))
                & models.Q(("reason__regex", "\\S")),
                name="retention_hold_authority_reason_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="legalhold",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("release_authority", ""),
                        ("release_reason", ""),
                        ("released_at__isnull", True),
                        ("released_by__isnull", True),
                    ),
                    models.Q(
                        ("release_authority__regex", "\\S"),
                        ("release_reason__regex", "\\S"),
                        ("released_at__isnull", False),
                        ("released_by__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="retention_hold_release_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="recordrelease",
            constraint=models.UniqueConstraint(
                condition=models.Q(("revoked_at__isnull", True)),
                fields=("version",),
                name="retention_release_one_active",
            ),
        ),
        migrations.AddConstraint(
            model_name="recordrelease",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("revoked_at__isnull", True), ("revoked_by__isnull", True)
                    ),
                    models.Q(
                        ("revoked_at__isnull", False),
                        ("revoked_by__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="retention_release_revocation_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="recordexport",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ["staff", "patient"])),
                name="retention_export_kind_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="recordexport",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("kind", "staff"),
                        ("patient_session__isnull", True),
                        ("requested_by__isnull", False),
                    ),
                    models.Q(
                        ("kind", "patient"),
                        ("patient_session__isnull", False),
                        ("requested_by__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="retention_export_actor_check",
            ),
        ),
        migrations.AddConstraint(
            model_name="recordexport",
            constraint=models.CheckConstraint(
                condition=models.Q(("manifest_digest__regex", "^[0-9a-f]{64}$")),
                name="retention_export_digest_check",
            ),
        ),
    ]
