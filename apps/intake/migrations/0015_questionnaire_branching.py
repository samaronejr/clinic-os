"""Allow physician publication and guard v2 assignment state in the database."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

SQL = """
ALTER POLICY template_write ON clinic_app.intake_questionnairetemplate
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(
   clinic_id, ARRAY['owner','clinic_admin','physician']));
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.questionnaire_template_state_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE definition jsonb;
BEGIN
 SELECT questions INTO definition FROM clinic_app.intake_questionnairetemplate
  WHERE id = NEW.template_id AND organization_id = NEW.organization_id
    AND clinic_id = NEW.clinic_id;
 IF jsonb_typeof(definition) = 'object'
    AND definition->>'state' IS DISTINCT FROM 'published' THEN
   RAISE EXCEPTION 'questionnaire is not published' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.questionnaire_template_state_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.questionnaire_template_state_guard()
 TO clinic_owner;
RESET ROLE;
CREATE TRIGGER questionnaire_template_state BEFORE INSERT
 ON clinic_app.intake_questionnaireresponse FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_template_state_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.questionnaire_template_state_guard()
 FROM clinic_owner;
RESET ROLE;
"""
REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DO $f$ BEGIN
 IF EXISTS (SELECT 1 FROM clinic_app.intake_questionnairetemplate
   WHERE jsonb_typeof(questions) = 'object') THEN
   RAISE EXCEPTION 'branching_rollback_is_restore' USING ERRCODE = '23514';
 END IF;
END $f$;
RESET ROLE;
DROP TRIGGER questionnaire_template_state ON clinic_app.intake_questionnaireresponse;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.questionnaire_template_state_guard();
RESET ROLE;
ALTER POLICY template_write ON clinic_app.intake_questionnairetemplate
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['owner','clinic_admin']));
"""


class Migration(migrations.Migration):
    """Keep all earlier list schemas usable without rewriting sensitive answers."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("intake", "0014_delegate_sessions"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
