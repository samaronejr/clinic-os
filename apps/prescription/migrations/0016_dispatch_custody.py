"""Distinguish known pre-dispatch work from a possibly accepted signing key."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.prescription.migrations._dispatch_custody_sql import SQL

_FIELD = "dispatch_started"
# No prior prepared operation can be proven unsent. PostgreSQL's missing-value
# default covers retained rows without rewriting encrypted artifacts under RLS.
_OPS = [
    migrations.AddField(
        model_name="signatureoperation",
        name=_FIELD,
        field=models.BooleanField(default=True, db_default=True),
    ),
    migrations.AlterField(
        model_name="signatureoperation",
        name=_FIELD,
        field=models.BooleanField(default=False, db_default=False),
    ),
]


class Migration(migrations.Migration):
    """Extend the existing state guard without granting any new table access."""

    dependencies: ClassVar = [("prescription", "0015_sncr_issuance")]
    operations: ClassVar = [
        SchemaStateBatch(database_operations=_OPS, state_operations=_OPS),
        migrations.RunSQL(SQL),
    ]
