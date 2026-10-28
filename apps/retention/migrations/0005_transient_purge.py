"""Irreversible transient destruction and permanent metadata-only tombstones."""

import uuid
from typing import ClassVar

from django.db import migrations, models
from django.db.models import deletion

from apps.retention.migrations._purge_audit_sql import SQL as AUDIT_SQL
from apps.retention.migrations._purge_execution_sql import SQL as EXECUTION_SQL
from apps.retention.migrations._purge_guard_sql import SQL as GUARD_SQL
from apps.retention.migrations._purge_restore_sql import SQL as RESTORE_SQL
from apps.retention.migrations._purge_scope_sql import SQL as SCOPE_SQL
from apps.retention.migrations._purge_task_sql import SQL as TASK_SQL

RECORD_CLASSES = (
    "ehr.encounter",
    "ehr.document_version",
    "ehr.clinical_attachment",
    "ehr.history_assessment",
    "ehr.problem",
    "ehr.allergy",
    "raw_audio",
    "ai_artifact",
    "transcript_partial",
)


class Migration(migrations.Migration):
    """No row deletions; rollback after destruction is restore plus tombstones."""

    dependencies: ClassVar = [
        ("retention", "0004_delegate_confidentiality"),
        ("identity", "0041_retention_authority"),
        ("scribe", "0004_purge_state"),
        ("ai", "0003_artifact_retention"),
        ("workflows", "0005_result_source_tasks"),
    ]
    operations: ClassVar = [
        migrations.RemoveConstraint(
            model_name="retentionpolicy", name="retention_policy_class_check"
        ),
        migrations.AddConstraint(
            model_name="retentionpolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(record_class__in=RECORD_CLASSES),
                name="retention_policy_class_check",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="legalhold", name="retention_hold_class_check"
        ),
        migrations.AddConstraint(
            model_name="legalhold",
            constraint=models.CheckConstraint(
                condition=models.Q(record_class__in=RECORD_CLASSES),
                name="retention_hold_class_check",
            ),
        ),
        migrations.CreateModel(
            name="ArtifactPurge",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        default=uuid.uuid4,
                        editable=False,
                        serialize=False,
                    ),
                ),
                ("record_class", models.CharField(max_length=24)),
                ("record_id", models.UUIDField()),
                ("state", models.CharField(max_length=16, default="retained")),
                ("due_at", models.DateTimeField()),
                ("backup_copy_expires_at", models.DateTimeField(null=True)),
                ("started_at", models.DateTimeField(null=True)),
                ("purged_at", models.DateTimeField(null=True)),
                ("target_count", models.PositiveIntegerField(default=0)),
                ("deleted_keys", models.JSONField(default=list)),
                (
                    "failure_code",
                    models.CharField(max_length=32, default="", blank=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "clinic",
                    models.ForeignKey("identity.clinic", on_delete=deletion.PROTECT),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        "identity.organization", on_delete=deletion.CASCADE
                    ),
                ),
                ("principal_id", models.UUIDField()),
                ("failure_task_id", models.UUIDField(null=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("clinic", "record_class", "record_id"),
                        name="retention_purge_target_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            record_class__in=(
                                "raw_audio",
                                "ai_artifact",
                                "transcript_partial",
                            )
                        )
                        & models.Q(
                            state__in=(
                                "retained",
                                "purge_due",
                                "purging",
                                "purged",
                                "blocked_hold",
                            )
                        )
                        & models.Q(failure_code__in=("", "storage_unavailable")),
                        name="retention_purge_values",
                    ),
                ],
            },
        ),
        migrations.RunSQL(
            SCOPE_SQL + GUARD_SQL + EXECUTION_SQL + TASK_SQL + AUDIT_SQL + RESTORE_SQL
        ),
    ]
