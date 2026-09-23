"""Questionnaire-specific patient/clinical RLS and immutable transition receipts."""

from typing import Final

SQL = """
GRANT SELECT ON clinic_app.intake_questionnairetemplate,
    clinic_app.intake_questionnaireresponse TO clinic_resolver;
GRANT INSERT ON clinic_app.intake_questionnaireevent TO clinic_resolver;
GRANT SELECT ON clinic_app.scheduling_appointment TO clinic_resolver;

SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.questionnaire_staff(requested_clinic uuid, roles text[])
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (
    SELECT 1 FROM clinic_app.identity_userclinicrole r
    JOIN clinic_app.identity_user u ON u.id = r.user_id AND u.is_active
    WHERE r.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
      AND r.organization_id =
          NULLIF(current_setting('app.current_tenant', true), '')::uuid
      AND r.clinic_id = requested_clinic AND r.role = ANY(roles)
 )
$f$;
CREATE FUNCTION clinic_app.questionnaire_patient_enrollment()
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT s.enrollment_id FROM clinic_app.intake_patientsession s
 JOIN clinic_app.intake_patientaccessgrant g ON g.id = s.grant_id
 WHERE s.id = NULLIF(current_setting('app.current_patient_session', true), '')::uuid
   AND s.revoked_at IS NULL AND g.revoked_at IS NULL
   AND s.expires_at > statement_timestamp()
   AND s.idle_expires_at > statement_timestamp()
   AND 'questionnaires' = ANY(s.operations)
$f$;
CREATE FUNCTION clinic_app.questionnaire_completion(
 requested_clinic uuid, requested_enrollment uuid)
RETURNS TABLE(response_id uuid, state varchar, revision integer)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT r.id, r.state, r.revision FROM clinic_app.intake_questionnaireresponse r
 WHERE r.clinic_id = requested_clinic AND r.enrollment_id = requested_enrollment
   AND clinic_app.questionnaire_staff(requested_clinic,
       ARRAY['owner', 'clinic_admin', 'receptionist', 'physician'])
 ORDER BY r.created_at, r.id
$f$;
CREATE FUNCTION clinic_app.questionnaire_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 RAISE EXCEPTION 'questionnaire history is immutable' USING ERRCODE = '23514';
END
$f$;
CREATE FUNCTION clinic_app.questionnaire_response_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF NOT EXISTS (
    SELECT 1 FROM clinic_app.intake_patientclinicenrollment e
    JOIN clinic_app.intake_questionnairetemplate t ON t.id = NEW.template_id
    WHERE e.id = NEW.enrollment_id AND e.patient_id = NEW.patient_id
      AND e.clinic_id = NEW.clinic_id AND e.organization_id = NEW.organization_id
      AND t.clinic_id = NEW.clinic_id AND t.organization_id = NEW.organization_id
 ) OR (NEW.appointment_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM clinic_app.scheduling_appointment a
    WHERE a.id = NEW.appointment_id AND a.patient_id = NEW.patient_id
      AND a.clinic_id = NEW.clinic_id AND a.organization_id = NEW.organization_id
 )) THEN
    RAISE EXCEPTION 'invalid questionnaire binding' USING ERRCODE = '23514';
 END IF;
 IF TG_OP = 'INSERT' THEN
    IF NEW.state <> 'draft' OR NEW.revision <> 1 OR NEW.answers <> '{}'::jsonb THEN
      RAISE EXCEPTION 'invalid initial response' USING ERRCODE = '23514';
    END IF;
 ELSE
    IF (NEW.organization_id, NEW.clinic_id, NEW.patient_id, NEW.enrollment_id,
        NEW.template_id, NEW.appointment_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.organization_id, OLD.clinic_id, OLD.patient_id, OLD.enrollment_id,
        OLD.template_id, OLD.appointment_id, OLD.created_at)
       OR NEW.revision <> OLD.revision + 1 THEN
      RAISE EXCEPTION 'immutable binding or stale revision' USING ERRCODE = '23514';
    END IF;
    IF OLD.state = 'submitted' THEN
      IF NEW.state <> 'draft' OR NEW.answers IS DISTINCT FROM OLD.answers
         OR btrim(NEW.reopen_reason) = ''
         OR NOT clinic_app.questionnaire_staff(NEW.clinic_id, ARRAY['physician']) THEN
        RAISE EXCEPTION 'clinical reopening required' USING ERRCODE = '23514';
      END IF;
    ELSIF NEW.enrollment_id IS DISTINCT FROM
          clinic_app.questionnaire_patient_enrollment()
       OR NEW.reopen_reason IS DISTINCT FROM OLD.reopen_reason THEN
      RAISE EXCEPTION 'patient session required' USING ERRCODE = '23514';
    END IF;
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.questionnaire_receipt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 INSERT INTO clinic_app.intake_questionnaireevent
   (id, organization_id, clinic_id, response_id, actor_id, patient_session_id,
    action, revision, answers, reason, created_at)
 VALUES (gen_random_uuid(), NEW.organization_id, NEW.clinic_id, NEW.id,
   NULLIF(current_setting('app.current_user_id', true), '')::uuid,
   NULLIF(current_setting('app.current_patient_session', true), '')::uuid,
   CASE WHEN TG_OP = 'INSERT' THEN 'assigned'
        WHEN NEW.state = 'submitted' THEN 'submitted'
        WHEN OLD.state = 'submitted' THEN 'reopened' ELSE 'draft_saved' END,
   NEW.revision, NEW.answers, NEW.reopen_reason, statement_timestamp());
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.questionnaire_staff(uuid, text[]),
 clinic_app.questionnaire_patient_enrollment(),
 clinic_app.questionnaire_completion(uuid, uuid),
 clinic_app.questionnaire_immutable(), clinic_app.questionnaire_response_guard(),
 clinic_app.questionnaire_receipt() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.questionnaire_staff(uuid, text[]),
 clinic_app.questionnaire_patient_enrollment(),
 clinic_app.questionnaire_completion(uuid, uuid)
 TO clinic_app;
-- The owner evaluates billing_staff policies on every maintenance write;
-- the boolean staff check must be callable there or the policy errors.
GRANT EXECUTE ON FUNCTION clinic_app.questionnaire_staff(uuid, text[])
 TO clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.questionnaire_immutable(),
 clinic_app.questionnaire_response_guard(), clinic_app.questionnaire_receipt()
 TO clinic_owner;
RESET ROLE;

CREATE TRIGGER questionnaire_template_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.intake_questionnairetemplate FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER questionnaire_event_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.intake_questionnaireevent FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER questionnaire_binding BEFORE INSERT OR UPDATE
 ON clinic_app.intake_questionnaireresponse FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_response_guard();
CREATE TRIGGER questionnaire_receipt AFTER INSERT OR UPDATE
 ON clinic_app.intake_questionnaireresponse FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_receipt();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.questionnaire_immutable(),
 clinic_app.questionnaire_response_guard(), clinic_app.questionnaire_receipt()
 FROM clinic_owner;
RESET ROLE;
"""

for table in ("questionnairetemplate", "questionnaireresponse", "questionnaireevent"):
    SQL += f"""
ALTER TABLE clinic_app.intake_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.intake_{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.intake_{table} TO clinic_owner
 USING (organization_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
 WITH CHECK (organization_id =
     NULLIF(current_setting('app.current_tenant', true), '')::uuid);
REVOKE ALL ON clinic_app.intake_{table} FROM PUBLIC, clinic_app;
GRANT SELECT ON clinic_app.intake_{table} TO clinic_app;
"""

SQL += """
GRANT INSERT ON clinic_app.intake_questionnairetemplate,
 clinic_app.intake_questionnaireresponse TO clinic_app;
GRANT UPDATE (answers, state, revision, submitted_at, reopen_reason, updated_at)
 ON clinic_app.intake_questionnaireresponse TO clinic_app;
CREATE POLICY template_read ON clinic_app.intake_questionnairetemplate
 FOR SELECT TO clinic_app
 USING (clinic_app.questionnaire_staff(
     clinic_id, ARRAY['owner','clinic_admin','physician'])
 OR EXISTS (SELECT 1 FROM clinic_app.intake_questionnaireresponse r
            WHERE r.template_id = intake_questionnairetemplate.id
              AND r.enrollment_id = clinic_app.questionnaire_patient_enrollment()));
CREATE POLICY template_write ON clinic_app.intake_questionnairetemplate
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id =
     NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['owner','clinic_admin']));
CREATE POLICY response_read ON clinic_app.intake_questionnaireresponse
 FOR SELECT TO clinic_app
 USING (clinic_app.questionnaire_staff(clinic_id, ARRAY['physician'])
 OR enrollment_id = clinic_app.questionnaire_patient_enrollment());
CREATE POLICY response_assign ON clinic_app.intake_questionnaireresponse
 FOR INSERT TO clinic_app
 WITH CHECK (clinic_app.questionnaire_staff(clinic_id, ARRAY['physician']));
CREATE POLICY response_write ON clinic_app.intake_questionnaireresponse
 FOR UPDATE TO clinic_app
 USING (clinic_app.questionnaire_staff(clinic_id, ARRAY['physician'])
 OR enrollment_id = clinic_app.questionnaire_patient_enrollment())
 WITH CHECK (clinic_app.questionnaire_staff(clinic_id, ARRAY['physician'])
 OR enrollment_id = clinic_app.questionnaire_patient_enrollment());
CREATE POLICY event_read ON clinic_app.intake_questionnaireevent
 FOR SELECT TO clinic_app
 USING (clinic_app.questionnaire_staff(clinic_id, ARRAY['physician']));
"""

REVERSE_SQL: Final = """
DROP TRIGGER questionnaire_receipt ON clinic_app.intake_questionnaireresponse;
DROP TRIGGER questionnaire_binding ON clinic_app.intake_questionnaireresponse;
DROP TRIGGER questionnaire_event_immutable ON clinic_app.intake_questionnaireevent;
DROP TRIGGER questionnaire_template_immutable
 ON clinic_app.intake_questionnairetemplate;
DROP POLICY event_read ON clinic_app.intake_questionnaireevent;
DROP POLICY response_write ON clinic_app.intake_questionnaireresponse;
DROP POLICY response_assign ON clinic_app.intake_questionnaireresponse;
DROP POLICY response_read ON clinic_app.intake_questionnaireresponse;
DROP POLICY template_write ON clinic_app.intake_questionnairetemplate;
DROP POLICY template_read ON clinic_app.intake_questionnairetemplate;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.questionnaire_receipt();
DROP FUNCTION clinic_app.questionnaire_response_guard();
DROP FUNCTION clinic_app.questionnaire_immutable();
DROP FUNCTION clinic_app.questionnaire_completion(uuid, uuid);
DROP FUNCTION clinic_app.questionnaire_patient_enrollment();
DROP FUNCTION clinic_app.questionnaire_staff(uuid, text[]);
RESET ROLE;
"""
