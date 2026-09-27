"""Create encrypted task history and install FORCE RLS in the same transaction."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.tenancy.fields

from ._workflow_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Additive schema; no plaintext conversion or in-place run migration."""

    initial = True

    dependencies: ClassVar = [
        ("comms", "0006_action_operations"),
        ("identity", "0016_task_workflows"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="Task",
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
                ("subject_ref", models.JSONField()),
                ("owner_role", models.CharField(blank=True, default="", max_length=20)),
                ("due_at", models.DateTimeField()),
                ("priority", models.CharField(default="normal", max_length=8)),
                ("state", models.CharField(default="open", max_length=16)),
                (
                    "escalation_policy_version",
                    models.PositiveSmallIntegerField(default=1),
                ),
                ("escalated_at", models.DateTimeField(null=True)),
                ("completion_evidence", models.JSONField(default=dict)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("idempotency_key", models.UUIDField()),
                ("fingerprint", models.CharField(max_length=64)),
                ("last_command_key", models.UUIDField(null=True)),
                (
                    "last_command_digest",
                    models.CharField(max_length=64, default="", blank=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
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
                    "depends_on",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="workflows.task",
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
                    "owner_user",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="TaskComment",
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
                    "body",
                    apps.tenancy.fields.EncryptedTextField(
                        purpose="workflows.comment.body"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "author",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
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
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="comments",
                        to="workflows.task",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="WorkflowDefinitionVersion",
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
                ("steps", models.JSONField()),
                ("idempotency_key", models.UUIDField()),
                ("fingerprint", models.CharField(max_length=64)),
                ("published_at", models.DateTimeField(auto_now_add=True)),
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
        migrations.CreateModel(
            name="WorkflowRun",
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
                ("context_refs", models.JSONField()),
                ("state", models.CharField(default="pending", max_length=16)),
                ("idempotency_key", models.UUIDField()),
                ("fingerprint", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "definition_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="workflows.workflowdefinitionversion",
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
                    "started_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="WorkflowStep",
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
                ("position", models.PositiveSmallIntegerField()),
                ("state", models.CharField(default="pending", max_length=16)),
                ("fencing_token", models.PositiveBigIntegerField(default=0)),
                ("claim_until", models.DateTimeField(null=True)),
                ("wake_at", models.DateTimeField(null=True)),
                ("error_code", models.CharField(blank=True, default="", max_length=24)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "created_task",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="workflows.task",
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="comms.integrationoperation",
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
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="steps",
                        to="workflows.workflowrun",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="task",
            index=models.Index(
                fields=["clinic", "state", "due_at"], name="workflows_task_due"
            ),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.UniqueConstraint(
                fields=("clinic", "idempotency_key"), name="workflows_task_command_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="workflows_task_scope_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "state__in",
                        ("open", "assigned", "in_progress", "done", "cancelled"),
                    )
                ),
                name="workflows_task_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ("checklist", "review", "follow_up"))),
                name="workflows_task_kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("priority__in", ("low", "normal", "high", "urgent"))
                ),
                name="workflows_task_priority",
            ),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=models.Q(("escalation_policy_version", 1)),
                name="workflows_task_escalation",
            ),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("owner_user__isnull", True), ("owner_role", ""), _connector="OR"
                ),
                name="workflows_task_one_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowdefinitionversion",
            constraint=models.UniqueConstraint(
                fields=("clinic", "key", "version"), name="workflows_definition_version"
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowdefinitionversion",
            constraint=models.UniqueConstraint(
                fields=("clinic", "idempotency_key"),
                name="workflows_publication_command",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowdefinitionversion",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="workflows_definition_scope",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowrun",
            constraint=models.UniqueConstraint(
                fields=("clinic", "idempotency_key"), name="workflows_run_command_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowrun",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="workflows_run_scope_uniq"
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowrun",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "state__in",
                        (
                            "pending",
                            "running",
                            "waiting",
                            "completed",
                            "failed",
                            "cancelled",
                        ),
                    )
                ),
                name="workflows_run_state",
            ),
        ),
        migrations.AddIndex(
            model_name="workflowstep",
            index=models.Index(
                fields=["state", "wake_at", "claim_until"], name="workflows_step_due"
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowstep",
            constraint=models.UniqueConstraint(
                fields=("run", "position"), name="workflows_step_position"
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowstep",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "state__in",
                        (
                            "pending",
                            "running",
                            "waiting",
                            "completed",
                            "failed",
                            "cancelled",
                        ),
                    )
                ),
                name="workflows_step_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowstep",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "error_code__in",
                        (
                            "",
                            "handler_rejected",
                            "operation_failed",
                            "operation_unknown",
                        ),
                    )
                ),
                name="workflows_step_error",
            ),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=models.Q(fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="workflows_task_fingerprint",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowdefinitionversion",
            constraint=models.CheckConstraint(
                condition=models.Q(fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="workflows_definition_digest",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowrun",
            constraint=models.CheckConstraint(
                condition=models.Q(fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="workflows_run_fingerprint",
            ),
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]
