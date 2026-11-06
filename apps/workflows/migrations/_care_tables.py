"""Schema operations for care plans, referrals, recalls and PRO assignments."""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.fields

OPERATIONS = [
    migrations.CreateModel(
        name="CarePlan",
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
            ("state", models.CharField(default="active", max_length=16)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("last_command_key", models.UUIDField(null=True)),
            (
                "last_command_digest",
                models.CharField(blank=True, default="", max_length=64),
            ),
            ("idempotency_key", models.UUIDField()),
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            (
                "clinician",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "encounter",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="ehr.encounter",
                ),
            ),
            (
                "enrollment",
                models.ForeignKey(
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
                "source_version",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="ehr.clinicaldocumentversion",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="CareAction",
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
            ("kind", models.CharField(max_length=24)),
            ("window_start", models.DateTimeField()),
            ("window_end", models.DateTimeField()),
            ("state", models.CharField(default="open", max_length=16)),
            ("owner_role", models.CharField(blank=True, default="", max_length=20)),
            ("review_state", models.CharField(default="none", max_length=16)),
            (
                "instruction",
                apps.tenancy.fields.EncryptedTextField(
                    null=True, purpose="workflows.careaction.instruction"
                ),
            ),
            ("revision", models.PositiveIntegerField(default=1)),
            ("last_command_key", models.UUIDField(null=True)),
            (
                "last_command_digest",
                models.CharField(blank=True, default="", max_length=64),
            ),
            ("idempotency_key", models.UUIDField()),
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            (
                "approved_by",
                models.ForeignKey(
                    null=True,
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
            (
                "origin_version",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="ehr.clinicaldocumentversion",
                ),
            ),
            (
                "owner_user",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            (
                "task",
                models.OneToOneField(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="workflows.task",
                ),
            ),
            (
                "plan",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="actions",
                    to="workflows.careplan",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="PROAssignment",
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
            ("instrument_key", models.CharField(max_length=16)),
            ("instrument_version", models.PositiveSmallIntegerField()),
            ("first_due_at", models.DateTimeField()),
            ("interval_days", models.PositiveSmallIntegerField()),
            ("occurrences", models.PositiveSmallIntegerField()),
            ("response_role", models.CharField(max_length=20)),
            ("staffing_policy_ref", models.CharField(max_length=64)),
            ("state", models.CharField(default="active", max_length=16)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("last_command_key", models.UUIDField(null=True)),
            (
                "last_command_digest",
                models.CharField(blank=True, default="", max_length=64),
            ),
            ("idempotency_key", models.UUIDField()),
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
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
                "enrollment",
                models.ForeignKey(
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
                "plan",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="pro_assignments",
                    to="workflows.careplan",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="CareRefusal",
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
            ("kind", models.CharField(max_length=16)),
            ("record_id", models.UUIDField()),
            ("attempts", models.PositiveIntegerField(default=1)),
            ("refused_at", models.DateTimeField()),
            ("retry_at", models.DateTimeField()),
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
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="PROOccurrence",
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
            ("sequence", models.PositiveSmallIntegerField()),
            ("due_at", models.DateTimeField()),
            ("state", models.CharField(default="pending", max_length=16)),
            (
                "answers",
                apps.tenancy.fields.EncryptedJSONField(
                    null=True, purpose="workflows.prooccurrence.answers"
                ),
            ),
            ("concerning", models.BooleanField(default=False)),
            ("recorded_at", models.DateTimeField(null=True)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("last_command_key", models.UUIDField(null=True)),
            (
                "last_command_digest",
                models.CharField(blank=True, default="", max_length=64),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            (
                "assignment",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="occurrence_set",
                    to="workflows.proassignment",
                ),
            ),
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
            (
                "response_task",
                models.OneToOneField(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="workflows.task",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="Recall",
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
            ("reason", models.CharField(max_length=24)),
            ("interval_days", models.PositiveSmallIntegerField()),
            ("anchor_at", models.DateTimeField()),
            ("due_at", models.DateTimeField()),
            ("owner_role", models.CharField(max_length=20)),
            ("state", models.CharField(default="scheduled", max_length=16)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("last_command_key", models.UUIDField(null=True)),
            (
                "last_command_digest",
                models.CharField(blank=True, default="", max_length=64),
            ),
            ("idempotency_key", models.UUIDField()),
            ("fingerprint", models.CharField(max_length=64)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
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
                "enrollment",
                models.ForeignKey(
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
                "task",
                models.OneToOneField(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="workflows.task",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="Referral",
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
            ("target_kind", models.CharField(max_length=16)),
            (
                "target_label",
                apps.tenancy.fields.EncryptedTextField(
                    null=True, purpose="workflows.referral.target_label"
                ),
            ),
            ("status", models.CharField(default="requested", max_length=16)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("last_command_key", models.UUIDField(null=True)),
            (
                "last_command_digest",
                models.CharField(blank=True, default="", max_length=64),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            (
                "action",
                models.OneToOneField(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="referral",
                    to="workflows.careaction",
                ),
            ),
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
                "document",
                models.OneToOneField(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="prescription.prescriptiondraft",
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
                "plan",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="referrals",
                    to="workflows.careplan",
                ),
            ),
        ],
    ),
    migrations.AddConstraint(
        model_name="careplan",
        constraint=models.UniqueConstraint(
            fields=("source_version",), name="workflows_careplan_source_uniq"
        ),
    ),
    migrations.AddConstraint(
        model_name="careplan",
        constraint=models.UniqueConstraint(
            fields=("clinic", "idempotency_key"), name="workflows_careplan_key"
        ),
    ),
    migrations.AddConstraint(
        model_name="careplan",
        constraint=models.UniqueConstraint(
            fields=("organization", "clinic", "id"), name="workflows_careplan_scope"
        ),
    ),
    migrations.AddConstraint(
        model_name="careplan",
        constraint=models.CheckConstraint(
            condition=models.Q(("state__in", ("active", "superseded"))),
            name="workflows_careplan_state",
        ),
    ),
    migrations.AddConstraint(
        model_name="careplan",
        constraint=models.CheckConstraint(
            condition=models.Q(("fingerprint__regex", "^[0-9a-f]{64}$")),
            name="workflows_careplan_digest",
        ),
    ),
    migrations.AddIndex(
        model_name="careaction",
        index=models.Index(
            fields=["clinic", "state", "window_end"], name="workflows_careaction_due"
        ),
    ),
    migrations.AddConstraint(
        model_name="careaction",
        constraint=models.UniqueConstraint(
            fields=("clinic", "idempotency_key"), name="workflows_careaction_key"
        ),
    ),
    migrations.AddConstraint(
        model_name="careaction",
        constraint=models.UniqueConstraint(
            fields=("organization", "clinic", "id"), name="workflows_careaction_scope"
        ),
    ),
    migrations.AddConstraint(
        model_name="careaction",
        constraint=models.CheckConstraint(
            condition=models.Q(
                (
                    "kind__in",
                    (
                        "return_window",
                        "order",
                        "referral",
                        "questionnaire",
                        "monitoring_task",
                        "patient_instruction",
                        "staff_coordination",
                    ),
                )
            ),
            name="workflows_careaction_kind",
        ),
    ),
    migrations.AddConstraint(
        model_name="careaction",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("state__in", ("open", "assigned", "overdue", "done", "cancelled"))
            ),
            name="workflows_careaction_state",
        ),
    ),
    migrations.AddConstraint(
        model_name="careaction",
        constraint=models.CheckConstraint(
            condition=models.Q(("review_state__in", ("none", "required", "confirmed"))),
            name="workflows_careaction_review",
        ),
    ),
    migrations.AddConstraint(
        model_name="careaction",
        constraint=models.CheckConstraint(
            condition=models.Q(("window_end__gt", models.F("window_start"))),
            name="workflows_careaction_window",
        ),
    ),
    migrations.AddConstraint(
        model_name="careaction",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("owner_user__isnull", True), ("owner_role", ""), _connector="OR"
            ),
            name="workflows_careaction_one_owner",
        ),
    ),
    migrations.AddConstraint(
        model_name="careaction",
        constraint=models.CheckConstraint(
            condition=models.Q(
                models.Q(("kind", "patient_instruction"), ("task__isnull", True)),
                models.Q(
                    models.Q(("kind", "patient_instruction"), _negated=True),
                    ("task__isnull", False),
                ),
                _connector="OR",
            ),
            name="workflows_careaction_task_shape",
        ),
    ),
    migrations.AddConstraint(
        model_name="careaction",
        constraint=models.CheckConstraint(
            condition=models.Q(
                models.Q(
                    ("instruction__isnull", False), ("kind", "patient_instruction")
                ),
                models.Q(
                    models.Q(("kind", "patient_instruction"), _negated=True),
                    ("instruction__isnull", True),
                ),
                _connector="OR",
            ),
            name="workflows_careaction_text_shape",
        ),
    ),
    migrations.AddConstraint(
        model_name="careaction",
        constraint=models.CheckConstraint(
            condition=models.Q(("fingerprint__regex", "^[0-9a-f]{64}$")),
            name="workflows_careaction_digest",
        ),
    ),
    migrations.AddConstraint(
        model_name="proassignment",
        constraint=models.UniqueConstraint(
            fields=("clinic", "idempotency_key"), name="workflows_pro_key"
        ),
    ),
    migrations.AddConstraint(
        model_name="proassignment",
        constraint=models.UniqueConstraint(
            fields=("organization", "clinic", "id"), name="workflows_pro_scope"
        ),
    ),
    migrations.AddConstraint(
        model_name="proassignment",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("instrument_key__in", ("phq9", "gad7")), ("instrument_version", 1)
            ),
            name="workflows_pro_instrument",
        ),
    ),
    migrations.AddConstraint(
        model_name="proassignment",
        constraint=models.CheckConstraint(
            condition=models.Q(("state__in", ("active", "completed"))),
            name="workflows_pro_state",
        ),
    ),
    migrations.AddConstraint(
        model_name="proassignment",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("interval_days__gte", 1),
                ("interval_days__lte", 365),
                ("occurrences__gte", 1),
                ("occurrences__lte", 12),
            ),
            name="workflows_pro_schedule",
        ),
    ),
    migrations.AddConstraint(
        model_name="proassignment",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("staffing_policy_ref__regex", "^[a-z][a-z0-9-]{2,62}$")
            ),
            name="workflows_pro_policy_ref",
        ),
    ),
    migrations.AddConstraint(
        model_name="proassignment",
        constraint=models.CheckConstraint(
            condition=models.Q(("fingerprint__regex", "^[0-9a-f]{64}$")),
            name="workflows_pro_digest",
        ),
    ),
    migrations.AddConstraint(
        model_name="proassignment",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("response_role__in", ("physician", "nurse", "allied_professional"))
            ),
            name="workflows_pro_response_role",
        ),
    ),
    migrations.AddIndex(
        model_name="prooccurrence",
        index=models.Index(
            fields=["state", "due_at"], name="workflows_prooccurrence_due"
        ),
    ),
    migrations.AddConstraint(
        model_name="prooccurrence",
        constraint=models.UniqueConstraint(
            fields=("assignment", "sequence"), name="workflows_prooccurrence_seq"
        ),
    ),
    migrations.AddConstraint(
        model_name="prooccurrence",
        constraint=models.CheckConstraint(
            condition=models.Q(("state__in", ("pending", "responded", "no_response"))),
            name="workflows_prooccurrence_state",
        ),
    ),
    migrations.AddConstraint(
        model_name="prooccurrence",
        constraint=models.CheckConstraint(
            condition=models.Q(
                models.Q(
                    ("answers__isnull", False),
                    ("response_task__isnull", False),
                    ("state", "responded"),
                ),
                models.Q(
                    ("answers__isnull", True),
                    ("concerning", False),
                    ("response_task__isnull", True),
                    ("state__in", ("pending", "no_response")),
                ),
                _connector="OR",
            ),
            name="workflows_prooccurrence_shape",
        ),
    ),
    migrations.AddIndex(
        model_name="recall",
        index=models.Index(fields=["state", "due_at"], name="workflows_recall_due"),
    ),
    migrations.AddConstraint(
        model_name="recall",
        constraint=models.UniqueConstraint(
            fields=("clinic", "idempotency_key"), name="workflows_recall_key"
        ),
    ),
    migrations.AddConstraint(
        model_name="recall",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("reason__in", ("routine_return", "test_follow_up", "chronic_review"))
            ),
            name="workflows_recall_reason",
        ),
    ),
    migrations.AddConstraint(
        model_name="recall",
        constraint=models.CheckConstraint(
            condition=models.Q(("state__in", ("scheduled", "materialized"))),
            name="workflows_recall_state",
        ),
    ),
    migrations.AddConstraint(
        model_name="recall",
        constraint=models.CheckConstraint(
            condition=models.Q(("interval_days__gte", 1), ("interval_days__lte", 730)),
            name="workflows_recall_interval",
        ),
    ),
    migrations.AddConstraint(
        model_name="recall",
        constraint=models.CheckConstraint(
            condition=models.Q(("fingerprint__regex", "^[0-9a-f]{64}$")),
            name="workflows_recall_digest",
        ),
    ),
    migrations.AddConstraint(
        model_name="carerefusal",
        constraint=models.UniqueConstraint(
            fields=("kind", "record_id"), name="workflows_carerefusal_record"
        ),
    ),
    migrations.AddConstraint(
        model_name="carerefusal",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("kind__in", ("recall", "overdue", "pro_missed", "stale"))
            ),
            name="workflows_carerefusal_kind",
        ),
    ),
    migrations.AddConstraint(
        model_name="carerefusal",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("retry_at__gt", models.F("refused_at")), ("attempts__gte", 1)
            ),
            name="workflows_carerefusal_retry",
        ),
    ),
    migrations.AddConstraint(
        model_name="referral",
        constraint=models.CheckConstraint(
            condition=models.Q(
                ("target_kind__in", ("specialty", "external", "unspecified"))
            ),
            name="workflows_referral_target",
        ),
    ),
    migrations.AddConstraint(
        model_name="referral",
        constraint=models.CheckConstraint(
            condition=models.Q(
                (
                    "status__in",
                    ("requested", "scheduled", "completed", "cancelled", "documented"),
                )
            ),
            name="workflows_referral_status",
        ),
    ),
    migrations.AddConstraint(
        model_name="referral",
        constraint=models.CheckConstraint(
            condition=models.Q(
                models.Q(("action__isnull", False), ("plan__isnull", False)),
                models.Q(("action__isnull", True), ("plan__isnull", True)),
                _connector="OR",
            ),
            name="workflows_referral_shape",
        ),
    ),
]
