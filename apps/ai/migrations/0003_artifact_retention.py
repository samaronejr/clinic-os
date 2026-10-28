"""Allow destruction of transient ciphertext without deleting provenance rows."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.contracts import PURPOSES
from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.tenancy.fields import EncryptedTextField


class Migration(migrations.Migration):
    """Irreversible protected-field change; rollback = restore plus tombstones."""

    atomic = False
    dependencies: ClassVar = [("ai", "0002_agent_execution")]
    _schema_operations: ClassVar = [
        migrations.AlterField(
            model_name="aiartifact",
            name="content",
            field=EncryptedTextField(purpose="ai.artifact.content.v1", null=True),
        ),
        migrations.AddField(
            model_name="aiartifact",
            name="purged_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RemoveConstraint(model_name="aiartifact", name="ai_artifact_values"),
        migrations.AddConstraint(
            model_name="aiartifact",
            constraint=models.CheckConstraint(
                condition=models.Q(purpose__in=PURPOSES)
                & models.Q(method__in=("complete", "stream", "transcribe", "extract"))
                & (
                    models.Q(
                        kind="input",
                        invocation__isnull=True,
                        retention_class="transient_input",
                    )
                    | models.Q(
                        kind="output",
                        invocation__isnull=False,
                        retention_class__in=("review_draft", "transcript_partial"),
                    )
                ),
                name="ai_artifact_values",
            ),
        ),
    ]
    operations: ClassVar = [
        SchemaStateBatch(
            database_operations=_schema_operations,
            state_operations=_schema_operations,
        ),
        migrations.RunPython(migrations.RunPython.noop),
    ]
