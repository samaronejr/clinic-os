"""Enveloped longitudinal facts; irreversible, rollback = restore (SC-17)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.fields
from apps.ehr.migrations import _longitudinal_sql, _note_access_sql, _timeline_sql
from apps.ehr.migrations._longitudinal_operations import ProtectedSchema

if TYPE_CHECKING:
    from django.db.migrations.operations.base import Operation

PROTECT = django.db.models.deletion.PROTECT
CASCADE = django.db.models.deletion.CASCADE
PROVENANCE = [
    ("reported_by_patient", "Reported by patient"),
    ("observed", "Observed"),
    ("imported", "Imported"),
    ("ai_drafted", "AI draft"),
    ("clinician_confirmed", "Clinician confirmed"),
]
ORIGINS = ("reported_by_patient", "observed", "imported", "ai_drafted")
CODE_LABELS = [
    ("systolic_bp", "Systolic blood pressure"),
    ("diastolic_bp", "Diastolic blood pressure"),
    ("heart_rate", "Heart rate"),
    ("respiratory_rate", "Respiratory rate"),
    ("temperature", "Temperature"),
    ("spo2", "Oxygen saturation"),
    ("weight", "Weight"),
    ("height", "Height"),
    ("bmi", "Body mass index"),
    ("pain_present", "Pain reported"),
]


CONTRACT = _longitudinal_sql.render(
    _longitudinal_sql.FUNCTIONS
    + _note_access_sql.FUNCTIONS
    + _timeline_sql.FUNCTIONS
    + _longitudinal_sql.POSTURE
    + _longitudinal_sql.POLICIES
    + _note_access_sql.POLICIES
    + _note_access_sql.TRIGGERS
    + _timeline_sql.POLICIES
    + _timeline_sql.TRIGGERS
    + _timeline_sql.BACKFILL
    + """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ehr_longitudinal_guard(),
 clinic_app.ehr_note_guard(), clinic_app.ehr_copy_finalization_guard(),
 clinic_app.ehr_timeline_enqueue(), clinic_app.questionnaire_immutable()
 FROM clinic_owner;
RESET ROLE;
"""
)


MODELS: list[Operation] = [
    migrations.CreateModel(
        name="RestrictedNote",
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
            ("clinic", models.ForeignKey(on_delete=PROTECT, to="identity.clinic")),
            (
                "document",
                models.OneToOneField(on_delete=PROTECT, to="ehr.clinicaldocument"),
            ),
            (
                "organization",
                models.ForeignKey(on_delete=CASCADE, to="identity.organization"),
            ),
            (
                "restricted_by",
                models.ForeignKey(
                    on_delete=PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="CopiedNoteBlock",
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
            ("section", models.CharField(max_length=16)),
            ("command_id", models.UUIDField()),
            ("inserted_revision", models.PositiveIntegerField()),
            ("reviewed_revision", models.PositiveIntegerField(null=True)),
            ("reviewed_at", models.DateTimeField(null=True)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("clinic", models.ForeignKey(on_delete=PROTECT, to="identity.clinic")),
            (
                "organization",
                models.ForeignKey(on_delete=CASCADE, to="identity.organization"),
            ),
            (
                "reviewed_by",
                models.ForeignKey(
                    null=True,
                    on_delete=PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "source",
                models.ForeignKey(
                    on_delete=PROTECT,
                    related_name="+",
                    to="ehr.clinicaldocumentversion",
                ),
            ),
            (
                "target",
                models.ForeignKey(
                    on_delete=PROTECT,
                    related_name="copied_blocks",
                    to="ehr.clinicaldocumentversion",
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("target", "command_id"), name="ehr_copy_command_unique"
                )
            ]
        },
    ),
    migrations.CreateModel(
        name="MedicationStatement",
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
            ("origin", models.CharField(choices=PROVENANCE, max_length=24)),
            ("provenance", models.CharField(choices=PROVENANCE, max_length=24)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("effective_at", models.DateTimeField()),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "details",
                apps.tenancy.fields.EncryptedJSONField(
                    purpose="ehr.medicationstatement.details"
                ),
            ),
            (
                "status",
                models.CharField(
                    choices=[
                        ("active", "Active"),
                        ("stopped", "Stopped"),
                        ("unknown", "Unknown"),
                    ],
                    max_length=16,
                ),
            ),
            ("reconciled_at", models.DateTimeField(null=True)),
            ("clinic", models.ForeignKey(on_delete=PROTECT, to="identity.clinic")),
            (
                "organization",
                models.ForeignKey(on_delete=CASCADE, to="identity.organization"),
            ),
            ("patient", models.ForeignKey(on_delete=PROTECT, to="intake.patient")),
            (
                "performer",
                models.ForeignKey(
                    on_delete=PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                ),
            ),
            (
                "reconciled_by",
                models.ForeignKey(
                    null=True,
                    on_delete=PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
        ],
        options={
            "constraints": [
                models.CheckConstraint(
                    condition=models.Q(status__in=["active", "stopped", "unknown"]),
                    name="ehr_medication_status",
                ),
                models.CheckConstraint(
                    condition=models.Q(
                        origin__in=ORIGINS,
                        provenance__in=[p[0] for p in PROVENANCE],
                        revision__gte=1,
                    ),
                    name="ehr_medication_provenance",
                ),
            ],
        },
    ),
    migrations.CreateModel(
        name="Observation",
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
            ("origin", models.CharField(choices=PROVENANCE, max_length=24)),
            ("provenance", models.CharField(choices=PROVENANCE, max_length=24)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("effective_at", models.DateTimeField()),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("code_set_version", models.PositiveSmallIntegerField(default=1)),
            ("kind", models.CharField(choices=CODE_LABELS, max_length=24)),
            ("unit", models.CharField(max_length=16)),
            (
                "value",
                apps.tenancy.fields.EncryptedJSONField(purpose="ehr.observation.value"),
            ),
            ("confirmed_at", models.DateTimeField(null=True)),
            ("clinic", models.ForeignKey(on_delete=PROTECT, to="identity.clinic")),
            (
                "confirmed_by",
                models.ForeignKey(
                    null=True,
                    on_delete=PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "organization",
                models.ForeignKey(on_delete=CASCADE, to="identity.organization"),
            ),
            ("patient", models.ForeignKey(on_delete=PROTECT, to="intake.patient")),
            (
                "performer",
                models.ForeignKey(
                    on_delete=PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                ),
            ),
        ],
        options={
            "constraints": [
                models.CheckConstraint(
                    condition=models.Q(
                        code_set_version=1, kind__in=[p[0] for p in CODE_LABELS]
                    ),
                    name="ehr_observation_codes_v1",
                ),
                models.CheckConstraint(
                    condition=models.Q(
                        origin__in=ORIGINS,
                        provenance__in=[p[0] for p in PROVENANCE],
                        revision__gte=1,
                    ),
                    name="ehr_observation_provenance",
                ),
            ],
        },
    ),
    migrations.CreateModel(
        name="NoteAccessGrant",
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
            ("revoked_at", models.DateTimeField(null=True)),
            ("clinic", models.ForeignKey(on_delete=PROTECT, to="identity.clinic")),
            (
                "granted_by",
                models.ForeignKey(
                    on_delete=PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                ),
            ),
            (
                "grantee",
                models.ForeignKey(
                    on_delete=PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                ),
            ),
            (
                "organization",
                models.ForeignKey(on_delete=CASCADE, to="identity.organization"),
            ),
            ("note", models.ForeignKey(on_delete=PROTECT, to="ehr.restrictednote")),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    condition=models.Q(revoked_at__isnull=True),
                    fields=("note", "grantee"),
                    name="ehr_note_one_active_grant",
                )
            ]
        },
    ),
    migrations.CreateModel(
        name="TimelineProjection",
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
            ("source_id", models.UUIDField()),
            ("kind", models.CharField(max_length=24)),
            ("effective_at", models.DateTimeField()),
            ("provenance", models.CharField(max_length=24)),
            ("source_revision", models.PositiveIntegerField(default=1)),
            ("clinic", models.ForeignKey(on_delete=PROTECT, to="identity.clinic")),
            (
                "encounter",
                models.ForeignKey(null=True, on_delete=PROTECT, to="ehr.encounter"),
            ),
            (
                "episode",
                models.ForeignKey(null=True, on_delete=PROTECT, to="ehr.episode"),
            ),
            (
                "organization",
                models.ForeignKey(on_delete=CASCADE, to="identity.organization"),
            ),
            ("patient", models.ForeignKey(on_delete=PROTECT, to="intake.patient")),
        ],
        options={
            "indexes": [
                models.Index(
                    fields=["clinic", "patient", "-effective_at", "id"],
                    name="ehr_timeline_patient_time",
                )
            ],
            "constraints": [
                models.UniqueConstraint(
                    fields=("kind", "source_id"), name="ehr_timeline_source_unique"
                )
            ],
        },
    ),
    migrations.CreateModel(
        name="TimelineRefresh",
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
            ("revision", models.PositiveBigIntegerField(default=1)),
            ("refreshed_revision", models.PositiveBigIntegerField(default=0)),
            ("refreshed_at", models.DateTimeField(null=True)),
            ("clinic", models.ForeignKey(on_delete=PROTECT, to="identity.clinic")),
            (
                "organization",
                models.ForeignKey(on_delete=CASCADE, to="identity.organization"),
            ),
            ("patient", models.ForeignKey(on_delete=PROTECT, to="intake.patient")),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "patient"), name="ehr_timeline_refresh_patient"
                ),
                models.CheckConstraint(
                    condition=models.Q(refreshed_revision__lte=models.F("revision")),
                    name="ehr_timeline_refresh_fence",
                ),
            ],
        },
    ),
]


class Migration(migrations.Migration):
    """No plaintext pass or reverse path; rollback = restore."""

    atomic = False
    dependencies: ClassVar = [
        ("ehr", "0013_template_v2"),
        ("identity", "0034_encounter_permission_bundle"),
        ("intake", "0014_delegate_sessions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations: ClassVar[list[Operation]] = [
        ProtectedSchema(
            database_operations=[*MODELS, migrations.RunSQL(CONTRACT)],
            state_operations=MODELS,
        ),
    ]
