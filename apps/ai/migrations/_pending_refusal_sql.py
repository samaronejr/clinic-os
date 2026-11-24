"""One content-free response for the original requester of a withheld turn."""

SQL = """
CREATE FUNCTION clinic_app.ai_pending_refusal(
 clinic uuid, requested uuid, purpose_name text, enrolled uuid,
 visit uuid, appointment uuid, session_binding text, kek text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE a clinic_app.ai_aiartifact; payload jsonb;
BEGIN
 SELECT * INTO a FROM clinic_app.ai_aiartifact ar
 WHERE ar.id=requested AND ar.clinic_id=clinic
   AND ar.organization_id=nullif(current_setting('app.current_tenant',true),'')::uuid
   AND ar.user_id=nullif(current_setting('app.current_user_id',true),'')::uuid
   AND ar.purged_at IS NULL AND ar.expires_at>statement_timestamp()
   AND ar.purpose IN ('brief','decision_support','scribe_asr','scribe_draft')
   AND (purpose_name IS NULL OR (ar.kind='input' AND ar.purpose=purpose_name))
   AND clinic_app.enterprise_staff_context()
   AND clinic_app.has_permission('clinical.read',ar.clinic_id,ar.patient_enrollment_id);
 IF NOT FOUND OR clinic_app.ai_pending_choice('ai_aiartifact',a.id)
 THEN RETURN false; END IF;
 IF session_binding IS NULL THEN RETURN true; END IF;
 IF a.kind<>'input' OR a.purpose NOT IN ('brief','decision_support')
  OR a.patient_enrollment_id IS DISTINCT FROM enrolled OR a.source_ref IS NULL
  OR (visit IS NOT NULL AND a.source_ref<>visit)
  OR (appointment IS NOT NULL AND NOT EXISTS(SELECT 1 FROM clinic_app.ehr_encounter e
    WHERE (e.organization_id,e.clinic_id,e.id,e.appointment_id)=
          (a.organization_id,clinic,a.source_ref,appointment)))
 THEN RETURN false; END IF;
 IF visit IS NULL AND appointment IS NULL AND EXISTS(
   SELECT 1 FROM clinic_app.ehr_encounter e
   JOIN clinic_app.intake_patientclinicenrollment p
    ON (p.organization_id,p.clinic_id,p.patient_id)=
       (e.organization_id,e.clinic_id,e.patient_id)
   WHERE (p.organization_id,p.clinic_id,p.id)=(a.organization_id,clinic,enrolled)
     AND e.state='open' AND e.id<>a.source_ref) THEN RETURN false; END IF;
 BEGIN
  payload:=convert_from(clinic_app.protected_decrypt(
    kek,'ai.artifact.content.v1',a.content),'UTF8')::jsonb;
  IF a.purpose='decision_support' THEN payload:=payload->'chart'; END IF;
  RETURN coalesce(payload->>'session'=session_binding,false);
 EXCEPTION WHEN data_exception OR SQLSTATE '39000' OR SQLSTATE 'TEN01'
   OR SQLSTATE 'TEN02' OR SQLSTATE 'TEN03' THEN RETURN false;
 END;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_pending_refusal(
 uuid,uuid,text,uuid,uuid,uuid,text,text)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_pending_refusal(
 uuid,uuid,text,uuid,uuid,uuid,text,text)
 TO clinic_app;
"""
