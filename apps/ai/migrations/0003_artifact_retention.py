"""Allow destruction of transient ciphertext without deleting provenance rows."""

from typing import ClassVar

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import ProjectState

from apps.ai.contracts import PURPOSES
from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.tenancy.fields import EncryptedTextField


class AddPurgeTimestamp(migrations.AddField):
    """Do not add a column left by an interrupted pre-fix upgrade twice."""

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Apply the database change only when needed on retry."""
        if not schema_editor.collect_sql:
            with schema_editor.connection.cursor() as cursor:
                columns = schema_editor.connection.introspection.get_table_description(
                    cursor, "ai_aiartifact"
                )
            if any(column.name == "purged_at" for column in columns):
                return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class RemoveArtifactCheck(migrations.RemoveConstraint):
    """Cover the pre-fix window between removing and replacing the check."""

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Apply the database change only when needed on retry."""
        if not schema_editor.collect_sql:
            with schema_editor.connection.cursor() as cursor:
                constraints = schema_editor.connection.introspection.get_constraints(
                    cursor, "ai_aiartifact"
                )
            if self.name not in constraints:
                return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class ArtifactSchemaBatch(SchemaStateBatch):
    """Commit the nullable column and checked retention vocabulary together."""

    atomic = True


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
        AddPurgeTimestamp(
            model_name="aiartifact",
            name="purged_at",
            field=models.DateTimeField(null=True),
        ),
        RemoveArtifactCheck(model_name="aiartifact", name="ai_artifact_values"),
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
        ArtifactSchemaBatch(
            database_operations=_schema_operations,
            state_operations=_schema_operations,
        ),
        migrations.RunPython(migrations.RunPython.noop),
    ]
