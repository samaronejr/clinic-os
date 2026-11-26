"""Install authenticated-score admission without back-certifying existing runs."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations import _scorer_commitment_sql
from apps.ai.migrations._state_batch import SchemaStateBatch


class ProtectedSchema(SchemaStateBatch):
    """Publish the new nullable commitment and both protected guards together."""

    atomic = True
    reversible = False


class Migration(migrations.Migration):
    """Never downgrade the writer to accepting unauthenticated observations."""

    atomic = False
    dependencies: ClassVar = [("ai", "0021_evaluation_origin")]
    operations: ClassVar = [
        ProtectedSchema(
            database_operations=[
                migrations.AddField(
                    "evaluationrun",
                    "producer_commitment",
                    models.BinaryField(null=True, editable=False),
                ),
                migrations.RunSQL(_scorer_commitment_sql.SQL),
            ],
            state_operations=[
                migrations.AddField(
                    "evaluationrun",
                    "producer_commitment",
                    models.BinaryField(null=True, editable=False),
                ),
            ],
        )
    ]
