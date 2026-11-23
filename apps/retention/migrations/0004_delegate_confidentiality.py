"""Explicit record audience; an unreviewed release stays private from delegates."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.retention.migrations._delegate_confidentiality_sql import SQL


class Migration(migrations.Migration):
    """Add the audience flag and prevent history from being silently widened."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("retention", "0003_protected_fields"),
        ("intake", "0014_delegate_sessions"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="recordrelease",
            name="restricted_from_delegates",
            field=models.BooleanField(default=True),
        ),
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
