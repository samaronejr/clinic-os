"""Permit subtraction from the unissued workflow successor without changing v1."""

from typing import ClassVar

from django.db import migrations

SQL = """
DO $body$
DECLARE legacy_check text;
BEGIN
 SELECT pg_get_constraintdef(oid) INTO STRICT legacy_check
 FROM pg_constraint
 WHERE conrelid='clinic_app.identity_rolegrant'::regclass
 AND conname='identity_rolegrant_bundle';
 IF left(legacy_check,7)<>'CHECK (' THEN
  RAISE EXCEPTION 'unexpected permission bundle constraint';
 END IF;
 ALTER TABLE clinic_app.identity_rolegrant
 DROP CONSTRAINT identity_rolegrant_bundle;
 EXECUTE 'ALTER TABLE clinic_app.identity_rolegrant
 ADD CONSTRAINT identity_rolegrant_bundle CHECK (' ||
 substring(legacy_check from 7) ||
 ' OR (bundle_version=2 AND (
 (role IN (''owner'',''org_admin'',''clinic_admin'',''clinic_manager'',
 ''physician'',''nurse'',''allied_professional'',''receptionist'',''scheduler'',
 ''finance'') AND permission IN (''tasks.view'',''tasks.assign'',''tasks.complete''))
 OR (role IN (''owner'',''org_admin'',''clinic_admin'',''clinic_manager'')
 AND permission=''tasks.reassign''))))';
END $body$;
"""


class Migration(migrations.Migration):
    """Keep the catalog's released legacy branch exactly as installed."""

    dependencies: ClassVar = [("identity", "0016_task_workflows")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
