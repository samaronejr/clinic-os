"""Boolean consent decision for an independently authorized financial guide."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_guide_consent(
 requested_clinic uuid, requested_guide uuid, requested_encounter uuid,
 retained_input uuid, kek text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 org uuid; enrollment uuid; patient uuid; input_body bytea; input_created timestamptz;
BEGIN
 SELECT g.organization_id,e.id,v.patient_id INTO org,enrollment,patient
  FROM clinic_app.insurance_guide g
  JOIN clinic_app.insurance_insurancemembership m
   ON (m.organization_id,m.clinic_id,m.id)=
      (g.organization_id,g.clinic_id,g.membership_id)
  JOIN clinic_app.intake_patientclinicenrollment e
   ON (e.organization_id,e.clinic_id,e.id)=
      (m.organization_id,m.clinic_id,m.enrollment_id)
  JOIN clinic_app.ehr_encounter v
   ON (v.organization_id,v.clinic_id,v.id,v.patient_id)=
      (g.organization_id,g.clinic_id,g.encounter_id,e.patient_id)
  WHERE (g.clinic_id,g.id,g.encounter_id)=
        (requested_clinic,requested_guide,requested_encounter)
   AND g.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
   AND NULLIF(current_setting('app.current_user_id',true),'') IS NOT NULL
   AND clinic_app.has_permission('charge.read',g.clinic_id,NULL)
   AND clinic_app.has_permission('tiss.read',g.clinic_id,NULL)
   AND clinic_app.has_permission('tiss.manage',g.clinic_id,NULL)
   AND NOT EXISTS (SELECT 1 FROM clinic_app.insurance_guide next_guide
    WHERE next_guide.predecessor_id=g.id)
   AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_aiusedisclosure d
    WHERE (d.organization_id,d.clinic_id,d.encounter_id)=
          (g.organization_id,g.clinic_id,g.encounter_id)
     AND (NOT d.informed OR d.refused));
 IF NOT FOUND THEN RETURN false; END IF;
 IF retained_input IS NOT NULL THEN
  SELECT i.content,i.created_at INTO input_body,input_created
   FROM clinic_app.ai_aiartifact i
   WHERE i.id=retained_input AND i.organization_id=org AND i.clinic_id=requested_clinic
    AND i.user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
    AND i.purpose='revenue_cycle' AND i.kind='input'
    AND i.purged_at IS NULL AND i.expires_at>statement_timestamp();
  IF NOT FOUND THEN RETURN false; END IF;
  -- Only the requester's own retained financial input ever reaches decryption.
  -- Neither the original content nor a parsing/decryption diagnostic is a result.
  BEGIN
   IF (convert_from(clinic_app.protected_decrypt(
        kek,'ai.artifact.content.v1',input_body),'UTF8')::jsonb->>'guide_id')
       IS DISTINCT FROM requested_guide::text THEN RETURN false; END IF;
  EXCEPTION WHEN data_exception OR SQLSTATE '39000' OR SQLSTATE 'TEN01'
   OR SQLSTATE 'TEN02' OR SQLSTATE 'TEN03' THEN RETURN false;
  END;
 END IF;
 -- No terms ever published retains the administrative guide's ordinary gate.
 -- Otherwise only the current receipt, already valid at creation, admits it.
 RETURN NOT EXISTS (
  SELECT 1 FROM (
   SELECT t.id FROM clinic_app.consent_consenttext t
   WHERE t.organization_id=org AND t.clinic_id=requested_clinic
    AND t.purpose='ai_assistance' ORDER BY t.version DESC LIMIT 1
  ) current_text WHERE NOT EXISTS (
    SELECT 1 FROM clinic_app.consent_consentacceptance a
    WHERE (a.organization_id,a.clinic_id,a.enrollment_id,a.patient_id,a.text_id)=
          (org,requested_clinic,enrollment,patient,current_text.id)
     AND (retained_input IS NULL OR a.accepted_at<=input_created)
     AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_consentrevocation r
      WHERE r.acceptance_id=a.id)));
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_guide_consent(uuid,uuid,uuid,uuid,text)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_guide_consent(uuid,uuid,uuid,uuid,text)
 TO clinic_app;
RESET ROLE;
"""

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ai_guide_consent(uuid,uuid,uuid,uuid,text);
RESET ROLE;
"""
