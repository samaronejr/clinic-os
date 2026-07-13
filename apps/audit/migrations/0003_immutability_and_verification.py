"""Make the audit ledger immutable under ordinary relation DML."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

INSTALL_IMMUTABILITY_SQL = """
CREATE FUNCTION clinic_app.audit_event_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '55000',
        MESSAGE = 'audit ledger is immutable';
END
$function$;

ALTER FUNCTION clinic_app.audit_event_reject_mutation()
    OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.audit_event_reject_mutation()
    FROM PUBLIC, clinic_app, clinic_resolver;

CREATE TRIGGER audit_event_immutable_row
BEFORE UPDATE OR DELETE ON clinic_app.audit_event
FOR EACH ROW
EXECUTE FUNCTION clinic_app.audit_event_reject_mutation();

CREATE TRIGGER audit_event_immutable_truncate
BEFORE TRUNCATE ON clinic_app.audit_event
FOR EACH STATEMENT
EXECUTE FUNCTION clinic_app.audit_event_reject_mutation();

REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON TABLE clinic_app.audit_event
    FROM PUBLIC, clinic_app, clinic_resolver;
"""

REMOVE_IMMUTABILITY_SQL = """
DROP TRIGGER IF EXISTS audit_event_immutable_truncate
    ON clinic_app.audit_event;
DROP TRIGGER IF EXISTS audit_event_immutable_row
    ON clinic_app.audit_event;
DROP FUNCTION IF EXISTS clinic_app.audit_event_reject_mutation();
"""


class Migration(migrations.Migration):
    """Install reversible owner-enforced audit mutation rejection."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("audit", "0002_audit_append"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            sql=INSTALL_IMMUTABILITY_SQL,
            reverse_sql=REMOVE_IMMUTABILITY_SQL,
        ),
    ]
