"""Assigned-clinician generations, source binding and retained review provenance."""

_sql = r"""
GRANT SELECT ON clinic_app.scribe_aidraft,clinic_app.scribe_draftsectionsuggestion,
 clinic_app.scribe_draftactionproposal,clinic_app.scribe_reviewdecision
 TO clinic_resolver;
GRANT SELECT(version_id,section_edit_epochs) ON clinic_app.ehr_drafteditstate
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.scribe_note_author(requested_version uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
 JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
 JOIN clinic_app.ehr_encounter e ON e.id=d.encounter_id
 JOIN clinic_app.identity_user actor
  ON actor.id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 WHERE v.id=requested_version
  AND actor.is_active
  AND v.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND v.author_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
  AND e.physician_id=v.author_id
  AND clinic_app.ehr_note_author(d.id) AND clinic_app.ehr_note_visible(d.id))
$f$;
CREATE FUNCTION clinic_app.scribe_note_visible(requested_version uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
 JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
 JOIN clinic_app.identity_user actor
  ON actor.id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 WHERE v.id=requested_version AND v.state<>'discarded'
  AND actor.is_active
  AND v.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND clinic_app.ehr_note_visible(d.id)
  AND ((clinic_app.ehr_assigned(d.encounter_id)
    AND (v.state<>'draft' OR v.author_id=
     NULLIF(current_setting('app.current_user_id',true),'')::uuid))
   OR (v.state IN ('finalized','superseded') AND clinic_app.ehr_care(d.encounter_id))))
$f$;
CREATE FUNCTION clinic_app.scribe_citation_allowed(
 requested_version uuid, requested_transcript uuid, reference jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE e clinic_app.ehr_encounter; t clinic_app.scribe_transcriptversion;
 identifier uuid; cited_version integer; source_kind text;
BEGIN
 IF NOT clinic_app.scribe_note_author(requested_version)
  OR jsonb_typeof(reference)<>'object'
  OR coalesce(reference->>'digest','')!~'^[0-9a-f]{64}$'
 THEN RETURN false; END IF;
 SELECT encounter.* INTO e FROM clinic_app.ehr_encounter encounter
 JOIN clinic_app.ehr_clinicaldocument d ON d.encounter_id=encounter.id
 JOIN clinic_app.ehr_clinicaldocumentversion v ON v.document_id=d.id
 WHERE v.id=requested_version;
 SELECT * INTO t FROM clinic_app.scribe_transcriptversion
 WHERE id=requested_transcript;
 IF t.id IS NULL OR NOT EXISTS(SELECT 1 FROM clinic_app.scribe_recordingsession s
  WHERE s.id=t.session_id AND s.encounter_id=e.id AND NOT s.discarded)
 THEN RETURN false; END IF;
 BEGIN
  identifier:=(reference->>'id')::uuid;
  source_kind:=reference->>'kind';
  IF source_kind='transcript' THEN
   RETURN coalesce(reference-'kind'-'id'-'version'-'revision'-'digest'='{}'::jsonb
    AND (reference->>'version')::uuid=t.id
    AND (reference->>'revision')::integer=t.revision
    AND t.coverage->'sources' @> jsonb_build_array(identifier)
    AND EXISTS(SELECT 1 FROM clinic_app.scribe_transcriptsegment s
     JOIN clinic_app.scribe_transcriptversion v ON v.id=s.version_id
     WHERE s.source_id=identifier AND s.session_id=t.session_id
      AND v.revision<=t.revision),false);
  END IF;
  cited_version:=(reference->>'version')::integer;
 EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
 THEN RETURN false;
 END;
 IF identifier IS NULL OR cited_version IS NULL OR cited_version<1
  OR reference-'section'-'kind'-'id'-'version'-'date'-'digest'-'evidence'<>'{}'::jsonb
 THEN RETURN false; END IF;
 RETURN EXISTS(
  WITH patients AS(SELECT patient_id FROM clinic_app.patient_alias_set(e.patient_id)),
  sources AS(
   SELECT 'encounter'::text AS kind,x.id,x.revision AS revision
    FROM clinic_app.ehr_encounter x
    WHERE x.clinic_id=e.clinic_id AND x.patient_id IN(SELECT patient_id FROM patients)
   UNION ALL SELECT 'note',v.id,v.version FROM clinic_app.ehr_clinicaldocumentversion v
    JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
    JOIN clinic_app.ehr_encounter x ON x.id=d.encounter_id
    WHERE x.clinic_id=e.clinic_id AND x.patient_id IN(SELECT patient_id FROM patients)
     AND v.state='finalized' AND clinic_app.ehr_note_visible(d.id)
   UNION ALL SELECT 'problem',x.id,x.version FROM clinic_app.ehr_problem x
    JOIN clinic_app.ehr_historyassessment h ON h.id=x.assessment_id
    WHERE h.clinic_id=e.clinic_id AND h.patient_id IN(SELECT patient_id FROM patients)
   UNION ALL SELECT 'allergy',x.id,x.version FROM clinic_app.ehr_allergy x
    JOIN clinic_app.ehr_historyassessment h ON h.id=x.assessment_id
    WHERE h.clinic_id=e.clinic_id AND h.patient_id IN(SELECT patient_id FROM patients)
   UNION ALL SELECT h.kind||'_assessment',h.id,h.revision
    FROM clinic_app.ehr_historyassessment h WHERE h.clinic_id=e.clinic_id
     AND h.patient_id IN(SELECT patient_id FROM patients) AND h.state='none_documented'
   UNION ALL SELECT 'medication',x.id,x.revision
    FROM clinic_app.ehr_medicationstatement x
    WHERE x.clinic_id=e.clinic_id AND x.patient_id IN(SELECT patient_id FROM patients)
   UNION ALL SELECT 'observation',x.id,x.revision FROM clinic_app.ehr_observation x
    WHERE x.clinic_id=e.clinic_id AND x.patient_id IN(SELECT patient_id FROM patients)
   UNION ALL SELECT 'result',v.id,v.number FROM clinic_app.orders_resultversion v
    JOIN clinic_app.orders_resultmatch m ON m.version_id=v.id
    JOIN clinic_app.orders_order o ON o.id=m.order_id
    JOIN clinic_app.intake_patientclinicenrollment p ON p.id=o.enrollment_id
    WHERE o.clinic_id=e.clinic_id AND p.patient_id IN(SELECT patient_id FROM patients)
   UNION ALL SELECT 'task',x.id,x.revision FROM clinic_app.workflows_task x
    WHERE x.clinic_id=e.clinic_id
     AND clinic_app.has_permission('tasks.view',e.clinic_id,NULL)
     AND ((x.subject_ref->>'kind'='enrollment' AND EXISTS(
       SELECT 1 FROM clinic_app.intake_patientclinicenrollment p
       WHERE p.id::text=x.subject_ref->>'id' AND p.clinic_id=e.clinic_id
        AND p.patient_id IN(SELECT patient_id FROM patients)))
      OR (x.subject_ref->>'kind'='appointment' AND EXISTS(
       SELECT 1 FROM clinic_app.scheduling_appointment a
       WHERE a.id::text=x.subject_ref->>'id' AND a.clinic_id=e.clinic_id
        AND a.patient_id IN(SELECT patient_id FROM patients)))))
  SELECT 1 FROM sources s WHERE s.kind=source_kind AND s.id=identifier
   AND s.revision=cited_version);
END
$f$;
CREATE FUNCTION clinic_app.scribe_draft_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE v clinic_app.ehr_clinicaldocumentversion; e clinic_app.ehr_encounter;
 a clinic_app.scribe_aidraft; s clinic_app.scribe_draftsectionsuggestion;
 i clinic_app.ai_aiinvocation; c clinic_app.ai_modelconfiguration;
 t clinic_app.scribe_transcriptversion; reference jsonb; section jsonb;
BEGIN
 IF TG_OP<>'INSERT' THEN
  RAISE EXCEPTION 'draft provenance is retained' USING ERRCODE='23514';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('scribe-draft:'||NEW.version_id,0));
 SELECT * INTO v FROM clinic_app.ehr_clinicaldocumentversion WHERE id=NEW.version_id;
 SELECT encounter.* INTO e FROM clinic_app.ehr_encounter encounter
 JOIN clinic_app.ehr_clinicaldocument d ON d.encounter_id=encounter.id
 WHERE d.id=v.document_id;
 IF v.id IS NULL OR v.state<>'draft' OR e.state<>'open'
  OR NEW.organization_id<>v.organization_id
  OR NOT clinic_app.scribe_note_author(v.id) THEN
  RAISE EXCEPTION 'draft authority denied' USING ERRCODE='42501';
 END IF;
 IF TG_TABLE_NAME='scribe_aidraft' THEN
  SELECT * INTO i FROM clinic_app.ai_aiinvocation WHERE id=NEW.invocation_id;
  SELECT * INTO t FROM clinic_app.scribe_transcriptversion WHERE id=NEW.transcript_id;
  IF (NEW.clinic_id,NEW.encounter_id,NEW.requested_by_id)
    IS DISTINCT FROM(e.clinic_id,e.id,v.author_id)
   OR i.id IS NULL OR (i.organization_id,i.clinic_id,i.user_id,i.purpose,i.status)
    IS DISTINCT FROM(v.organization_id,e.clinic_id,v.author_id,
     'scribe_draft'::text,'reserved'::text)
   OR i.operation_id IS NULL OR NEW.base_revision<>v.revision
   OR NEW.generation<>coalesce((SELECT max(generation) FROM clinic_app.scribe_aidraft
    WHERE version_id=v.id),0)+1
   OR NEW.base_epochs IS DISTINCT FROM coalesce((SELECT section_edit_epochs
    FROM clinic_app.ehr_drafteditstate WHERE version_id=v.id),
    '{"subjective":0,"objective":0,"assessment":0,"plan":0}'::jsonb)
   OR t.id IS NULL OR t.state<>'final'
   OR NOT EXISTS(SELECT 1 FROM clinic_app.scribe_recordingsession r
    WHERE r.id=t.session_id AND r.encounter_id=e.id AND NOT r.discarded)
   OR EXISTS(SELECT 1 FROM clinic_app.scribe_transcriptversion newer
    WHERE newer.session_id=t.session_id AND newer.revision>t.revision)
   OR NOT EXISTS(SELECT 1 FROM clinic_app.consent_consentacceptance acceptance
    JOIN clinic_app.consent_consenttext text ON text.id=acceptance.text_id
    JOIN clinic_app.intake_patientclinicenrollment p ON p.id=acceptance.enrollment_id
    JOIN clinic_app.consent_aiusedisclosure disclosure ON disclosure.encounter_id=e.id
    WHERE acceptance.id=NEW.consent_id AND acceptance.organization_id=v.organization_id
     AND acceptance.clinic_id=e.clinic_id AND p.patient_id=e.patient_id
     AND i.inputs_ref_id IN(SELECT input.id FROM clinic_app.ai_aiartifact input
      WHERE input.patient_enrollment_id=p.id AND input.kind='input')
     AND text.purpose='ai_assistance' AND disclosure.informed AND NOT disclosure.refused
     AND NOT EXISTS(SELECT 1 FROM clinic_app.consent_consentrevocation r
      WHERE r.acceptance_id=acceptance.id)
     AND NOT EXISTS(SELECT 1 FROM clinic_app.consent_consenttext newer
      WHERE newer.clinic_id=text.clinic_id AND newer.purpose=text.purpose
       AND newer.version>text.version))
   OR jsonb_typeof(NEW.sections) IS DISTINCT FROM 'array'
   OR jsonb_array_length(NEW.sections) NOT BETWEEN 1 AND 4
   OR jsonb_array_length(NEW.sections)<>(SELECT count(DISTINCT value)
    FROM jsonb_array_elements(NEW.sections))
   OR jsonb_typeof(NEW.source_refs) IS DISTINCT FROM 'array'
   OR jsonb_array_length(NEW.source_refs) NOT BETWEEN 1 AND 250
   OR octet_length(NEW.base_content) NOT BETWEEN 1 AND 1048576
  THEN RAISE EXCEPTION 'invalid draft generation' USING ERRCODE='23514'; END IF;
  FOR section IN SELECT * FROM jsonb_array_elements(NEW.sections) LOOP
   IF section NOT IN('"subjective"'::jsonb,'"objective"'::jsonb,
    '"assessment"'::jsonb,'"plan"'::jsonb) THEN
    RAISE EXCEPTION 'invalid draft section' USING ERRCODE='23514'; END IF;
  END LOOP;
  FOR reference IN SELECT * FROM jsonb_array_elements(NEW.source_refs) LOOP
   IF NOT clinic_app.scribe_citation_allowed(v.id,t.id,reference) THEN
    RAISE EXCEPTION 'invalid draft citation' USING ERRCODE='23514'; END IF;
  END LOOP;
 ELSE
  IF TG_TABLE_NAME='scribe_reviewdecision' THEN
   SELECT * INTO s FROM clinic_app.scribe_draftsectionsuggestion
    WHERE id=NEW.suggestion_id;
   SELECT * INTO a FROM clinic_app.scribe_aidraft WHERE id=s.draft_id;
  ELSE
   SELECT * INTO a FROM clinic_app.scribe_aidraft WHERE id=NEW.draft_id;
  END IF;
  SELECT * INTO i FROM clinic_app.ai_aiinvocation WHERE id=a.invocation_id;
  IF a.id IS NULL OR a.version_id<>v.id OR a.organization_id<>NEW.organization_id
   OR i.status<>'succeeded' THEN
   RAISE EXCEPTION 'invalid draft binding' USING ERRCODE='23514'; END IF;
  IF TG_TABLE_NAME='scribe_reviewdecision' THEN
   SELECT * INTO c FROM clinic_app.ai_modelconfiguration WHERE id=i.configuration_id;
   IF s.id IS NULL OR s.version_id<>v.id OR NEW.section<>s.section
    OR NEW.reviewed_by_id<>v.author_id
    OR NEW.action NOT IN('accepted','modified','rejected')
    OR NEW.result_revision<>v.revision OR NEW.result_sha256<>v.content_sha256
    OR NEW.result_revision NOT IN(NEW.expected_revision,NEW.expected_revision+1)
    OR (NEW.action='rejected' AND NEW.result_revision<>NEW.expected_revision)
    OR NEW.request_digest!~'^[0-9a-f]{64}$'
    OR (NEW.action='rejected' AND NEW.applied_text IS NOT NULL)
    OR (NEW.action IN('accepted','modified') AND (
     NEW.applied_text IS NULL
     OR octet_length(NEW.applied_text) NOT BETWEEN 1 AND 262144))
    OR NEW.capability_snapshot IS DISTINCT FROM jsonb_build_object(
     'purpose','scribe_draft','capability',c.capability_key,
     'configuration_id',c.id,'version',i.config_version)
   THEN RAISE EXCEPTION 'invalid clinician review' USING ERRCODE='23514'; END IF;
   IF NEW.action<>'rejected' AND EXISTS(SELECT 1 FROM clinic_app.scribe_aidraft newer
    WHERE newer.version_id=v.id AND newer.generation>a.generation
     AND newer.sections @> jsonb_build_array(s.section)) THEN
    RAISE EXCEPTION 'draft generation superseded' USING ERRCODE='23514'; END IF;
  ELSE
   IF jsonb_typeof(NEW.citations) IS DISTINCT FROM 'array'
    OR jsonb_array_length(NEW.citations) NOT BETWEEN 1 AND 8
    OR NOT a.source_refs @> NEW.citations
    OR octet_length(NEW.text) NOT BETWEEN 1 AND 262144
   THEN RAISE EXCEPTION 'invalid draft source' USING ERRCODE='23514'; END IF;
   IF TG_TABLE_NAME='scribe_draftsectionsuggestion' THEN
    IF NOT a.sections @> jsonb_build_array(NEW.section)
     OR NEW.assertion_class NOT IN('reported','observed','hypothesis')
     OR EXISTS(SELECT 1 FROM clinic_app.scribe_aidraft newer
      WHERE newer.version_id=v.id AND newer.generation>a.generation
       AND newer.sections @> jsonb_build_array(NEW.section)) THEN
     RAISE EXCEPTION 'invalid section suggestion' USING ERRCODE='23514'; END IF;
   ELSIF NEW.kind NOT IN('order','task') OR NEW.ordinal NOT BETWEEN 0 AND 7 THEN
    RAISE EXCEPTION 'invalid separate proposal' USING ERRCODE='23514';
   END IF;
  END IF;
 END IF;
 NEW.created_at:=statement_timestamp();
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.scribe_note_author(uuid),
 clinic_app.scribe_note_visible(uuid),clinic_app.scribe_citation_allowed(uuid,uuid,jsonb),
 clinic_app.scribe_draft_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.scribe_note_author(uuid),
 clinic_app.scribe_note_visible(uuid) TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.scribe_draft_guard() TO clinic_owner;
RESET ROLE;
"""

for _table in (
    "aidraft",
    "draftsectionsuggestion",
    "draftactionproposal",
    "reviewdecision",
):
    _reader = (
        "scribe_note_visible" if _table == "reviewdecision" else "scribe_note_author"
    )
    _sql += f"""
ALTER TABLE clinic_app.scribe_{_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.scribe_{_table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.scribe_{_table} FROM PUBLIC,clinic_app;
GRANT SELECT,INSERT ON clinic_app.scribe_{_table} TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.scribe_{_table} TO clinic_owner
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
  WITH CHECK(
   organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY draft_read ON clinic_app.scribe_{_table} FOR SELECT TO clinic_app
 USING(clinic_app.{_reader}(version_id));
CREATE POLICY draft_insert ON clinic_app.scribe_{_table} FOR INSERT TO clinic_app
 WITH CHECK(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND clinic_app.scribe_note_author(version_id));
CREATE TRIGGER draft_binding BEFORE INSERT OR UPDATE OR DELETE
  ON clinic_app.scribe_{_table} FOR EACH ROW
  EXECUTE FUNCTION clinic_app.scribe_draft_guard();
CREATE TRIGGER draft_no_truncate BEFORE TRUNCATE ON clinic_app.scribe_{_table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.scribe_draft_guard();
"""

_sql += """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.scribe_draft_guard() FROM clinic_owner;
RESET ROLE;
"""

SQL = "BEGIN;\n" + _sql + "\nCOMMIT;"
