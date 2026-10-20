"""Atomically bind participant mode transitions to canonical audit receipts."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.tenancy.migration_operations import AddConstraint

from ._mode_transition_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Install the sole receipt writer and validate every mode-event binding."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("audit", "0005_clinic_metadata_v2"),
        ("teleconsult", "0005_event_authority"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(SQL, REVERSE_SQL),
        AddConstraint(
            model_name="teleconsultevent",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(kind__in=("audio_only", "video_restored"))
                    & models.Q(mode_audit_seq__isnull=False)
                )
                | (
                    ~models.Q(kind__in=("audio_only", "video_restored"))
                    & models.Q(mode_audit_seq__isnull=True)
                ),
                name="teleconsult_mode_audit_binding",
            ),
        ),
    ]
