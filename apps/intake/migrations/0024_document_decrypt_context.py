"""Restore patient-session scope after opening retained prescription content."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.prescription_document_open(
 kek pg_catalog.text,
 organization_id pg_catalog.uuid,
 purpose pg_catalog.text,
 envelope pg_catalog.bytea
)
RETURNS pg_catalog.bytea
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 prior_tenant pg_catalog.text;
 plaintext pg_catalog.bytea;
BEGIN
 prior_tenant := pg_catalog.current_setting('app.current_tenant', true);
 PERFORM pg_catalog.set_config(
   'app.current_tenant', organization_id::pg_catalog.text, true);
 BEGIN
   plaintext := clinic_app.tenant_decrypt(kek, purpose, envelope);
 EXCEPTION WHEN OTHERS THEN
   PERFORM pg_catalog.set_config(
     'app.current_tenant', COALESCE(prior_tenant, ''), true);
   RAISE;
 END;
 PERFORM pg_catalog.set_config(
   'app.current_tenant', COALESCE(prior_tenant, ''), true);
 RETURN plaintext;
END
$f$;
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Keep alias read and audit authority unchanged after successful decryption."""

    dependencies: ClassVar = [
        ("intake", "0023_duplicate_lookup_indexes"),
        ("prescription", "0010_protected_documents"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL)]
