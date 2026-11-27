"""Interop protected import schema: irreversible; rollback = restore."""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import apps.tenancy.fields
from apps.ai.migrations._state_batch import SchemaStateBatch

MODELS: list[Operation] = [
    migrations.CreateModel(
        name="ImportBatch",
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
            ("sequence", models.PositiveIntegerField()),
            ("dataset", models.CharField(max_length=16)),
            ("first_row", models.PositiveIntegerField()),
            ("last_row", models.PositiveIntegerField()),
            ("rows_sha256", models.CharField(max_length=64)),
            ("state", models.CharField(default="pending", max_length=16)),
            ("committed_count", models.PositiveIntegerField(default=0)),
            ("committed_at", models.DateTimeField(null=True)),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
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
                "requested_by",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="ImportJob",
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
            ("source_kind", models.CharField(max_length=8)),
            ("size_bytes", models.PositiveBigIntegerField()),
            ("source_sha256", models.CharField(max_length=64)),
            ("idempotency_key", models.UUIDField()),
            ("fingerprint", models.CharField(max_length=64)),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "created_by",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
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
        name="ImportFile",
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
            ("role", models.CharField(max_length=16)),
            (
                "path",
                apps.tenancy.fields.EncryptedTextField(
                    null=True, purpose="interop.importfile.path"
                ),
            ),
            ("sha256", models.CharField(max_length=64)),
            ("size_bytes", models.PositiveBigIntegerField()),
            ("media_type", models.CharField(max_length=128)),
            ("scan_verdict", models.CharField(max_length=16)),
            ("scan_reason", models.CharField(blank=True, default="", max_length=32)),
            (
                "data",
                apps.tenancy.fields.EncryptedBytesField(
                    purpose="interop.importfile.data"
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
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
                "job",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="interop.importjob"
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="ImportedRecord",
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
            ("dataset", models.CharField(max_length=16)),
            ("row_number", models.PositiveIntegerField()),
            ("kind", models.CharField(max_length=16)),
            (
                "source_record_id",
                apps.tenancy.fields.EncryptedTextField(
                    purpose="interop.importedrecord.source_record_id"
                ),
            ),
            (
                "original_author",
                apps.tenancy.fields.EncryptedTextField(
                    null=True, purpose="interop.importedrecord.original_author"
                ),
            ),
            ("original_recorded_at", models.DateTimeField(null=True)),
            (
                "content",
                apps.tenancy.fields.EncryptedJSONField(
                    null=True, purpose="interop.importedrecord.content"
                ),
            ),
            ("content_sha256", models.CharField(max_length=64)),
            ("identifier_count", models.PositiveSmallIntegerField(default=0)),
            (
                "batch",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="interop.importbatch",
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
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
                "file",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    to="interop.importfile",
                ),
            ),
            (
                "job",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="interop.importjob"
                ),
            ),
        ],
    ),
    migrations.AddField(
        model_name="importbatch",
        name="job",
        field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT, to="interop.importjob"
        ),
    ),
    migrations.CreateModel(
        name="HiddenEnrollment",
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
            ("hidden_at", models.DateTimeField()),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "enrollment",
                models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
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
                "job",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="interop.importjob"
                ),
            ),
        ],
        options={
            "abstract": False,
        },
    ),
    migrations.CreateModel(
        name="ImportMapping",
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
            ("code", models.CharField(max_length=40)),
            ("version", models.PositiveIntegerField()),
            ("source_kind", models.CharField(max_length=8)),
            (
                "document",
                apps.tenancy.fields.EncryptedJSONField(
                    purpose="interop.importmapping.document"
                ),
            ),
            ("document_sha256", models.CharField(max_length=64)),
            ("suggested", models.BooleanField(default=False)),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "created_by",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
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
        name="ImportTransition",
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
            ("sequence", models.PositiveIntegerField()),
            ("state", models.CharField(max_length=16)),
            (
                "report",
                apps.tenancy.fields.EncryptedJSONField(
                    null=True, purpose="interop.importtransition.report"
                ),
            ),
            ("digest", models.CharField(blank=True, default="", max_length=64)),
            ("row_count", models.PositiveIntegerField(default=0)),
            ("valid_count", models.PositiveIntegerField(default=0)),
            ("rejected_count", models.PositiveIntegerField(default=0)),
            ("duplicate_count", models.PositiveIntegerField(default=0)),
            ("rollback_window_hours", models.PositiveIntegerField(null=True)),
            ("reason", models.CharField(blank=True, default="", max_length=32)),
            (
                "actor",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "job",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="interop.importjob"
                ),
            ),
            (
                "mapping",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    to="interop.importmapping",
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
    migrations.AddConstraint(
        model_name="importjob",
        constraint=models.UniqueConstraint(
            fields=("clinic", "idempotency_key"), name="interop_job_command"
        ),
    ),
    migrations.AddConstraint(
        model_name="importjob",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("source_kind__in", ("csv", "xlsx", "zip", "api")),
                ("size_bytes__gte", 1),
                ("source_sha256__regex", "^[0-9a-f]{64}$"),
                ("fingerprint__regex", "^[0-9a-f]{64}$"),
            ),
            name="interop_job_values",
        ),
    ),
    migrations.AddConstraint(
        model_name="importfile",
        constraint=models.UniqueConstraint(
            condition=models.Q(("role", "source")),
            fields=("job",),
            name="interop_file_one_source",
        ),
    ),
    migrations.AddConstraint(
        model_name="importfile",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("role__in", ("source", "attachment")),
                ("scan_verdict__in", ("not_applicable", "clean", "rejected")),
                ("sha256__regex", "^[0-9a-f]{64}$"),
                ("size_bytes__gte", 1),
                models.Q(
                    models.Q(("role", "source"), ("scan_verdict", "not_applicable")),
                    models.Q(
                        ("role", "attachment"),
                        ("scan_verdict__in", ("clean", "rejected")),
                    ),
                    _connector="OR",
                ),
            ),
            name="interop_file_values",
        ),
    ),
    migrations.AddConstraint(
        model_name="importedrecord",
        constraint=models.UniqueConstraint(
            fields=("job", "dataset", "row_number"), name="interop_record_row"
        ),
    ),
    migrations.AddConstraint(
        model_name="importedrecord",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("dataset__in", ("patients", "history")),
                ("kind__in", ("patient", "note", "observation", "attachment")),
                ("row_number__gte", 1),
                ("content_sha256__regex", "^[0-9a-f]{64}$"),
                models.Q(
                    models.Q(
                        ("dataset", "patients"),
                        ("file__isnull", True),
                        ("kind", "patient"),
                    ),
                    models.Q(
                        ("dataset", "history"),
                        ("file__isnull", True),
                        ("kind__in", ("note", "observation")),
                    ),
                    models.Q(
                        ("dataset", "history"),
                        ("file__isnull", False),
                        ("kind", "attachment"),
                    ),
                    _connector="OR",
                ),
            ),
            name="interop_record_values",
        ),
    ),
    migrations.AddConstraint(
        model_name="importbatch",
        constraint=models.UniqueConstraint(
            fields=("job", "sequence"), name="interop_batch_sequence"
        ),
    ),
    migrations.AddConstraint(
        model_name="importbatch",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("sequence__gte", 1),
                ("dataset__in", ("patients", "history")),
                ("state__in", ("pending", "committed")),
                ("first_row__gte", 1),
                ("last_row__gte", models.F("first_row")),
                ("rows_sha256__regex", "^[0-9a-f]{64}$"),
            ),
            name="interop_batch_values",
        ),
    ),
    migrations.AddConstraint(
        model_name="importmapping",
        constraint=models.UniqueConstraint(
            fields=("clinic", "code", "version"), name="interop_mapping_version"
        ),
    ),
    migrations.AddConstraint(
        model_name="importmapping",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("version__gte", 1),
                ("source_kind__in", ("csv", "xlsx", "zip", "api")),
                ("code__regex", "^[a-z0-9][a-z0-9_-]{0,39}$"),
                ("document_sha256__regex", "^[0-9a-f]{64}$"),
            ),
            name="interop_mapping_values",
        ),
    ),
    migrations.AddConstraint(
        model_name="importtransition",
        constraint=models.UniqueConstraint(
            fields=("job", "sequence"), name="interop_transition_sequence"
        ),
    ),
    migrations.AddConstraint(
        model_name="importtransition",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("sequence__gte", 1),
                (
                    "state__in",
                    (
                        "uploaded",
                        "profiled",
                        "mapped",
                        "dry_run",
                        "approved",
                        "committed",
                        "rolled_back",
                    ),
                ),
                models.Q(
                    ("digest", ""), ("digest__regex", "^[0-9a-f]{64}$"), _connector="OR"
                ),
                models.Q(
                    ("reason", ""),
                    (
                        "reason__in",
                        (
                            "abandoned",
                            "data_quality",
                            "wrong_source",
                            "cutover_cancelled",
                        ),
                    ),
                    _connector="OR",
                ),
            ),
            name="interop_transition_values",
        ),
    ),
]


class ProtectedSchema(SchemaStateBatch):
    """New encrypted fields are irreversible; rollback requires restore."""

    atomic = True
    reversible = False
