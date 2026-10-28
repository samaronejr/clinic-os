"""Allow room capture only through a current consented recording session."""

from typing import ClassVar

from django.db import migrations, models

from apps.teleconsult.migrations import _consented_capture_sql
from apps.tenancy.migration_operations import AddConstraint


class Migration(migrations.Migration):
    """Preserve old guards and relax only the permanently-disabled flag check."""

    dependencies: ClassVar = [
        ("teleconsult", "0006_mode_audit"),
        ("scribe", "0001_initial"),
    ]
    operations: ClassVar = [
        migrations.RemoveConstraint(
            model_name="teleconsultroom", name="teleconsult_room_capture_disabled"
        ),
        AddConstraint(
            model_name="teleconsultroom",
            constraint=models.CheckConstraint(
                condition=models.Q(transcription_enabled=False),
                name="teleconsult_room_transcription_disabled",
            ),
        ),
        migrations.RunSQL(
            _consented_capture_sql.SQL, _consented_capture_sql.REVERSE_SQL
        ),
    ]
