"""Extend care reads across aliases; keep all original write/immutability guards."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.ehr_care(requested_encounter uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $function$
 SELECT EXISTS (SELECT 1 FROM clinic_app.ehr_encounter e
 WHERE e.id=requested_encounter
 AND e.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.questionnaire_staff(e.clinic_id,ARRAY['physician'])
 AND (EXISTS (
     SELECT 1 FROM clinic_app.scheduling_appointment a
     WHERE a.clinic_id=e.clinic_id
       AND a.patient_id IN (
           SELECT lineage.patient_id FROM clinic_app.patient_alias_set(e.patient_id)
               lineage
       )
       AND a.practitioner_id=NULLIF(current_setting('app.current_user_id',true),
           '')::uuid
 ) OR EXISTS (
     SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
     JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
     JOIN clinic_app.ehr_encounter other ON other.id=d.encounter_id
     WHERE other.clinic_id=e.clinic_id
       AND other.patient_id IN (
           SELECT lineage.patient_id FROM clinic_app.patient_alias_set(e.patient_id)
               lineage
       )
       AND v.author_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 )))
$function$;
ALTER FUNCTION clinic_app.ehr_care(uuid) OWNER TO clinic_resolver;

CREATE OR REPLACE FUNCTION clinic_app.ehr_history_care(requested_encounter uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $function$
 SELECT clinic_app.ehr_care(requested_encounter) OR EXISTS (
     SELECT 1 FROM clinic_app.ehr_encounter e
     JOIN clinic_app.ehr_historyassessment h ON h.clinic_id=e.clinic_id
       AND h.organization_id=e.organization_id
       AND h.patient_id IN (
           SELECT lineage.patient_id FROM clinic_app.patient_alias_set(e.patient_id)
               lineage
       )
     WHERE e.id=requested_encounter
       AND e.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
       AND clinic_app.questionnaire_staff(e.clinic_id,ARRAY['physician'])
       AND h.author_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 )
$function$;
ALTER FUNCTION clinic_app.ehr_history_care(uuid) OWNER TO clinic_resolver;
RESET ROLE;
"""
