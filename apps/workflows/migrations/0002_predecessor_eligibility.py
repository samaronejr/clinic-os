"""Resolve completion eligibility without exposing a predecessor's task row."""

from typing import ClassVar

from django.db import migrations

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.workflows_predecessor_ready(
 clinic uuid, requested_task uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('tasks.complete',clinic,NULL) AND EXISTS (
 SELECT 1 FROM clinic_app.workflows_task child
 JOIN clinic_app.workflows_task parent ON parent.id=child.depends_on_id
 AND parent.clinic_id=child.clinic_id
 AND parent.organization_id=child.organization_id
 WHERE child.id=requested_task AND child.clinic_id=clinic
 AND child.organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.workflows_owned(child.clinic_id,child.owner_user_id,child.owner_role)
 AND parent.state='done')
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_predecessor_ready(uuid,uuid)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_predecessor_ready(uuid,uuid)
 TO clinic_app;
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Add a narrow eligibility boolean; never grant predecessor row access."""

    dependencies: ClassVar = [("workflows", "0001_task_workflows")]
    operations: ClassVar = [
        migrations.RunSQL(
            SQL,
            reverse_sql=(
                "SET LOCAL ROLE clinic_resolver;"
                "DROP FUNCTION clinic_app.workflows_predecessor_ready(uuid,uuid);"
                "RESET ROLE;"
            ),
        )
    ]
