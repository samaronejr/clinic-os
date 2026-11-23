"""Distinguish known pre-dispatch work from a possibly accepted signing key."""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.prescription.migrations._independent_signature_sql import SQL as PRIOR

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

_GUARD = """
 IF TG_OP = 'INSERT' AND NEW.dispatch_started THEN
    RAISE EXCEPTION 'unexpected dispatch on insert' USING ERRCODE='23514';
 END IF;
 IF TG_OP = 'UPDATE' THEN
   IF NEW.dispatch_started IS DISTINCT FROM OLD.dispatch_started THEN
     IF OLD.state='prepared' AND NOT OLD.dispatch_started AND NEW.dispatch_started
       AND (to_jsonb(NEW)-'dispatch_started')=(to_jsonb(OLD)-'dispatch_started')
     THEN RETURN NEW; END IF;
     RAISE EXCEPTION 'immutable signing dispatch' USING ERRCODE='23514';
   END IF;
   IF NEW.state='failed' AND (
     (NEW.failure_reason='superseded' AND OLD.state<>'draft')
     OR (NEW.failure_reason='abandoned' AND NOT (OLD.state='draft'
       OR (OLD.state='prepared' AND NOT OLD.dispatch_started)))
     OR (OLD.state='prepared' AND OLD.dispatch_started
       AND NEW.failure_reason<>'provider_rejected')
   ) THEN
     RAISE EXCEPTION 'unresolved signature operation' USING ERRCODE='23514';
   END IF;
 END IF;
"""


class Migration(migrations.Migration):
    """Extend the existing state guard without granting any new table access."""

    dependencies: ClassVar = [("prescription", "0015_sncr_issuance")]
    operations: ClassVar = [
        SchemaStateBatch(database_operations=_OPS, state_operations=_OPS),
        migrations.RunSQL(
            PRIOR.replace("BEGIN\n", "BEGIN\n" + _GUARD, 1)
            + "GRANT UPDATE (dispatch_started) ON "
            "clinic_app.prescription_signatureoperation TO clinic_app;"
        ),
    ]
