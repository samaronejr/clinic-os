"""Unified released-history reads; disposal and original release bindings stay."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.retention_care(
 requested_clinic uuid,requested_patient uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $function$
 SELECT EXISTS (
   SELECT 1 FROM clinic_app.intake_patientclinicenrollment n
   WHERE n.clinic_id=requested_clinic AND n.patient_id=requested_patient
     AND n.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
     AND clinic_app.questionnaire_staff(requested_clinic,ARRAY['physician'])
     AND (EXISTS (
       SELECT 1 FROM clinic_app.scheduling_appointment a
       WHERE a.clinic_id=requested_clinic
         AND a.patient_id IN (
           SELECT lineage.patient_id FROM clinic_app.patient_alias_set
               (requested_patient) lineage
         )
         AND a.practitioner_id=NULLIF(current_setting('app.current_user_id',true),
             '')::uuid
     ) OR EXISTS (
       SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
       JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
       JOIN clinic_app.ehr_encounter e ON e.id=d.encounter_id
       WHERE e.clinic_id=requested_clinic
         AND e.patient_id IN (
           SELECT lineage.patient_id FROM clinic_app.patient_alias_set
               (requested_patient) lineage
         )
         AND v.author_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
     ))
 )
$function$;

CREATE OR REPLACE FUNCTION clinic_app.retention_patient_releases()
RETURNS TABLE(release_id uuid,version_id uuid,document_id uuid,encounter_id uuid,
 version_number int4,state text,content_digest text,finalized_at timestamptz,
 amendment_of_version int4,author_label text,content bytea,released_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $function$
 SELECT r.id,v.id,d.id,e.id,v.version,v.state::text,v.content_digest::text,
   v.finalized_at,base.version,u.username::text,v.content,r.created_at
 FROM clinic_app.retention_recordrelease r
 JOIN clinic_app.ehr_clinicaldocumentversion v ON v.id=r.version_id
 JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
 JOIN clinic_app.ehr_encounter e ON e.id=d.encounter_id
 JOIN clinic_app.identity_user u ON u.id=v.author_id
 LEFT JOIN clinic_app.ehr_clinicaldocumentversion base ON base.id=v.amendment_of_id
 WHERE r.revoked_at IS NULL
   AND (NOT clinic_app.patient_is_delegate() OR NOT r.restricted_from_delegates)
   AND EXISTS (
     SELECT 1 FROM clinic_app.retention_records_session() s
     WHERE s.organization_id=r.organization_id AND s.clinic_id=r.clinic_id
       AND r.patient_id IN (
         SELECT lineage.patient_id FROM clinic_app.patient_alias_set(s.patient_id)
             lineage
       )
   )
 ORDER BY e.created_at,d.id,v.version
$function$;
RESET ROLE;
"""
