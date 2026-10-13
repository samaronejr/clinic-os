"""Recognize explicit revocation without changing stale rotation refusals."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from ._session_rotation_sql import GUARD_SQL, SQL


class Migration(migrations.Migration):
    """Retain all existing session records and their immutable identities."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0025_offboarding_internal_reader"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.AddField(
            model_name="sessionrecord",
            name="superseded_at",
            field=models.DateTimeField(editable=False, null=True),
        ),
        migrations.AddConstraint(
            model_name="sessionrecord",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(superseded_at__isnull=True)
                    | models.Q(revoked_at__isnull=False)
                ),
                name="identity_session_superseded_is_revoked",
            ),
        ),
        migrations.RunSQL(GUARD_SQL),
        migrations.RunSQL(SQL),
    ]
