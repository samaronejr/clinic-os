"""Exact-clinic retention authority, hold serialization and transient selectors."""

SQL = r"""

GRANT SELECT,INSERT,UPDATE ON clinic_app.retention_artifactpurge TO clinic_resolver;
GRANT UPDATE(state,wrapped_key,encrypted_data) ON clinic_app.scribe_audiochunk
 TO clinic_resolver;
GRANT UPDATE(content,purged_at) ON clinic_app.ai_aiartifact TO clinic_resolver;
ALTER TABLE clinic_app.retention_artifactpurge
 ADD CONSTRAINT retention_purge_clinic_fk FOREIGN KEY(organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
REVOKE ALL ON clinic_app.retention_artifactpurge FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT ON clinic_app.retention_artifactpurge TO clinic_app;
ALTER TABLE clinic_app.retention_artifactpurge ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.retention_artifactpurge FORCE ROW LEVEL SECURITY;
CREATE POLICY purge_owner ON clinic_app.retention_artifactpurge TO clinic_owner
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK(organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid);
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.retention_authorized(clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce(clinic_app.retention_principal_scope(
  NULLIF(current_setting('app.current_principal',true),'')::uuid,clinic)
  =NULLIF(current_setting('app.current_tenant',true),'')::uuid,false)
$f$;
REVOKE ALL ON FUNCTION clinic_app.retention_authorized(uuid) FROM PUBLIC;
CREATE FUNCTION clinic_app.retention_sweep_scopes()
RETURNS TABLE(principal_id uuid,clinic_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT p.id,p.clinic_id FROM clinic_app.identity_serviceprincipal p
 WHERE clinic_app.retention_principal_scope(p.id,p.clinic_id)=p.organization_id
 ORDER BY p.clinic_id,p.id
$f$;
REVOKE ALL ON FUNCTION clinic_app.retention_sweep_scopes() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_sweep_scopes() TO clinic_agent;
CREATE FUNCTION clinic_app.retention_related_hold(
 target_class text,target_id uuid,held_class text,held_id uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT (target_class=held_class AND target_id=held_id)
 OR (target_class='raw_audio' AND EXISTS(
  SELECT 1 FROM clinic_app.scribe_recordingsession s
  WHERE s.id=target_id AND (
   (held_class='ehr.encounter' AND held_id=s.encounter_id)
   OR (held_class='ehr.document_version' AND EXISTS(
    SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
    JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
    WHERE v.id=held_id AND d.encounter_id=s.encounter_id)))))
 OR (target_class IN ('ai_artifact','transcript_partial') AND EXISTS(
  SELECT 1 FROM clinic_app.ai_aiartifact a
  JOIN clinic_app.intake_patientclinicenrollment p ON p.id=a.patient_enrollment_id
  JOIN clinic_app.ehr_encounter e ON e.patient_id=p.patient_id
   AND e.clinic_id=a.clinic_id
  WHERE a.id=target_id AND (
   (held_class='ehr.encounter' AND held_id=e.id)
   OR (held_class='ehr.document_version' AND EXISTS(
    SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
    JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
    WHERE v.id=held_id AND d.encounter_id=e.id)))))
$f$;
REVOKE ALL ON FUNCTION clinic_app.retention_related_hold(text,uuid,text,uuid)
 FROM PUBLIC;
CREATE FUNCTION clinic_app.retention_held(clinic uuid,record_class text,record_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.retention_legalhold h
 WHERE h.clinic_id=$1 AND h.released_at IS NULL
 AND clinic_app.retention_related_hold($2,$3,h.record_class,
  h.record_id))
$f$;
REVOKE ALL ON FUNCTION clinic_app.retention_held(uuid,text,uuid) FROM PUBLIC;
CREATE FUNCTION clinic_app.retention_due(clinic uuid,record_class text,record_id uuid)
RETURNS timestamptz LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE days integer; approved boolean; source_time timestamptz; result timestamptz;
BEGIN
 SELECT p.retention_days,true INTO days,approved
 FROM clinic_app.retention_retentionpolicy p WHERE p.clinic_id=clinic
 AND p.record_class=retention_due.record_class AND p.state='approved';
 IF approved AND days IS NULL THEN RETURN NULL; END IF;
 IF record_class='raw_audio' THEN
  IF NOT coalesce(approved,false) THEN
   SELECT c.raw_audio_retention_days INTO days
   FROM clinic_app.identity_clinicconfiguration c WHERE c.clinic_id=clinic
   ORDER BY c.version DESC LIMIT 1;
   days:=coalesce(days,7);
  END IF;
  SELECT (
   SELECT min(v.finalized_at) FROM clinic_app.ehr_clinicaldocumentversion v
   JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
   WHERE d.encounter_id=s.encounter_id AND v.finalized_at IS NOT NULL
  ) INTO source_time
  FROM clinic_app.scribe_recordingsession s WHERE s.id=record_id
  AND s.clinic_id=clinic AND s.state IN ('stopped','stopped_revoked');
  result:=source_time+days*interval '1 day';
 ELSIF record_class IN ('ai_artifact','transcript_partial') THEN
  SELECT CASE WHEN coalesce(approved,false) THEN a.created_at+days*interval '1 day'
   ELSE a.expires_at END INTO result FROM clinic_app.ai_aiartifact a
  WHERE a.id=record_id AND a.clinic_id=clinic
  AND ((record_class='transcript_partial' AND a.retention_class='transcript_partial')
   OR (record_class='ai_artifact' AND a.retention_class IN
    ('transient_input','review_draft')));
 END IF;
 RETURN result;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_due(uuid,text,uuid) FROM PUBLIC;
CREATE FUNCTION clinic_app.retention_purge_visible(requested uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.retention_artifactpurge p
 WHERE p.id=requested
 AND p.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND (clinic_app.questionnaire_staff(p.clinic_id,ARRAY['owner','clinic_admin'])
 OR (p.record_class='raw_audio' AND EXISTS(
  SELECT 1 FROM clinic_app.scribe_recordingsession s WHERE s.id=p.record_id
  AND clinic_app.scribe_assigned(s.id)))
 OR (p.record_class IN ('ai_artifact','transcript_partial') AND EXISTS(
  SELECT 1 FROM clinic_app.ai_aiartifact a WHERE a.id=p.record_id
  AND a.user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
  AND clinic_app.ai_staff(a.clinic_id,a.purpose,a.patient_enrollment_id)))))
$f$;
REVOKE ALL ON FUNCTION clinic_app.retention_purge_visible(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_purge_visible(uuid)
 TO clinic_app,clinic_owner;
RESET ROLE;
CREATE POLICY purge_read ON clinic_app.retention_artifactpurge
 FOR SELECT TO clinic_app USING(clinic_app.retention_purge_visible(id));
SET LOCAL ROLE clinic_resolver;

CREATE OR REPLACE FUNCTION clinic_app.retention_record_scope(
 requested_class pg_catalog.text, requested_record pg_catalog.uuid)
RETURNS TABLE(record_clinic pg_catalog.uuid,
              record_organization pg_catalog.uuid,
              record_created pg_catalog.timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 -- Canonical authority: the record must live in the caller's tenant and the
 -- caller must hold a manager role in the record's own clinic. Foreign or
 -- unmatched principals receive zero rows, the same as an unknown record.

 IF requested_class='raw_audio' THEN
  RETURN QUERY SELECT s.clinic_id,s.organization_id,s.created_at
  FROM clinic_app.scribe_recordingsession s WHERE s.id=requested_record
  AND s.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND clinic_app.questionnaire_staff(s.clinic_id,ARRAY['owner','clinic_admin']);
 ELSIF requested_class IN ('ai_artifact','transcript_partial') THEN
  RETURN QUERY SELECT a.clinic_id,a.organization_id,a.created_at
  FROM clinic_app.ai_aiartifact a WHERE a.id=requested_record
  AND a.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND ((requested_class='transcript_partial'
    AND a.retention_class='transcript_partial')
   OR (requested_class='ai_artifact'
    AND a.retention_class IN ('transient_input','review_draft')))
  AND clinic_app.questionnaire_staff(a.clinic_id,ARRAY['owner','clinic_admin']);
 ELSIF requested_class = 'ehr.encounter' THEN
   RETURN QUERY SELECT e.clinic_id, e.organization_id, e.created_at
     FROM clinic_app.ehr_encounter e WHERE e.id = requested_record
     AND e.organization_id =
       NULLIF(current_setting('app.current_tenant', true), '')::uuid
     AND clinic_app.questionnaire_staff(e.clinic_id,
       ARRAY['owner','clinic_admin']);
 ELSIF requested_class = 'ehr.document_version' THEN
   RETURN QUERY SELECT e.clinic_id, e.organization_id, v.created_at
     FROM clinic_app.ehr_clinicaldocumentversion v
     JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
     JOIN clinic_app.ehr_encounter e ON e.id = d.encounter_id
     WHERE v.id = requested_record
     AND e.organization_id =
       NULLIF(current_setting('app.current_tenant', true), '')::uuid
     AND clinic_app.questionnaire_staff(e.clinic_id,
       ARRAY['owner','clinic_admin']);
 ELSIF requested_class = 'ehr.clinical_attachment' THEN
   RETURN QUERY SELECT a.clinic_id, a.organization_id, a.created_at
     FROM clinic_app.ehr_clinicalattachment a WHERE a.id = requested_record
     AND a.organization_id =
       NULLIF(current_setting('app.current_tenant', true), '')::uuid
     AND clinic_app.questionnaire_staff(a.clinic_id,
       ARRAY['owner','clinic_admin']);
 ELSIF requested_class = 'ehr.history_assessment' THEN
   RETURN QUERY SELECT h.clinic_id, h.organization_id, h.created_at
     FROM clinic_app.ehr_historyassessment h WHERE h.id = requested_record
     AND h.organization_id =
       NULLIF(current_setting('app.current_tenant', true), '')::uuid
     AND clinic_app.questionnaire_staff(h.clinic_id,
       ARRAY['owner','clinic_admin']);
 ELSIF requested_class = 'ehr.problem' THEN
   RETURN QUERY SELECT h.clinic_id, h.organization_id, h.created_at
     FROM clinic_app.ehr_problem p
     JOIN clinic_app.ehr_historyassessment h ON h.id = p.assessment_id
     WHERE p.id = requested_record
     AND h.organization_id =
       NULLIF(current_setting('app.current_tenant', true), '')::uuid
     AND clinic_app.questionnaire_staff(h.clinic_id,
       ARRAY['owner','clinic_admin']);
 ELSIF requested_class = 'ehr.allergy' THEN
   RETURN QUERY SELECT h.clinic_id, h.organization_id, h.created_at
     FROM clinic_app.ehr_allergy a
     JOIN clinic_app.ehr_historyassessment h ON h.id = a.assessment_id
     WHERE a.id = requested_record
     AND h.organization_id =
       NULLIF(current_setting('app.current_tenant', true), '')::uuid
     AND clinic_app.questionnaire_staff(h.clinic_id,
       ARRAY['owner','clinic_admin']);
 END IF;
END
$f$;
CREATE OR REPLACE FUNCTION clinic_app.retention_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 scope RECORD;
 encounter_row clinic_app.ehr_encounter;
 version_row clinic_app.ehr_clinicaldocumentversion;
 latest integer;
BEGIN
 IF TG_TABLE_NAME = 'retention_retentionpolicy' THEN
   PERFORM pg_advisory_xact_lock(hashtextextended('retention:'||NEW.clinic_id,0));
   IF TG_OP = 'INSERT' THEN
     IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
        WHERE c.id = NEW.clinic_id AND c.organization_id = NEW.organization_id)
        OR NEW.record_class NOT IN ('ehr.encounter','ehr.document_version',
          'ehr.clinical_attachment','ehr.history_assessment','ehr.problem',
          'ehr.allergy','raw_audio','ai_artifact','transcript_partial')
        OR NEW.state <> 'proposed' OR NEW.approved_by_id IS NOT NULL
        OR NEW.approved_at IS NOT NULL OR NEW.retired_by_id IS NOT NULL
        OR NEW.retired_at IS NOT NULL
        OR (NEW.retention_days IS NOT NULL AND NEW.retention_days < 0)
        OR NEW.proposed_by_id <>
          NULLIF(current_setting('app.current_user_id', true), '')::uuid
        OR NOT clinic_app.questionnaire_staff(NEW.clinic_id,
          ARRAY['owner','clinic_admin']) THEN
       RAISE EXCEPTION 'invalid policy binding' USING ERRCODE = '23514';
     END IF;
     SELECT max(version) INTO latest FROM clinic_app.retention_retentionpolicy
       WHERE clinic_id = NEW.clinic_id AND record_class = NEW.record_class;
     IF NEW.version <> coalesce(latest, 0) + 1 THEN
       RAISE EXCEPTION 'invalid policy version' USING ERRCODE = '23514';
     END IF;
   ELSE
     IF (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.record_class,NEW.version,
         NEW.retention_days,NEW.proposed_by_id,NEW.created_at)
        IS DISTINCT FROM
        (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.record_class,OLD.version,
         OLD.retention_days,OLD.proposed_by_id,OLD.created_at) THEN
       RAISE EXCEPTION 'immutable policy identity' USING ERRCODE = '23514';
     END IF;
     IF OLD.state = 'proposed' AND NEW.state = 'approved' THEN
       IF (NEW.retired_by_id,NEW.retired_at) IS DISTINCT FROM
          (OLD.retired_by_id,OLD.retired_at)
          OR NEW.approved_by_id <>
            NULLIF(current_setting('app.current_user_id', true), '')::uuid
          OR NEW.approved_at IS NULL
          OR NOT clinic_app.questionnaire_staff(NEW.clinic_id,
            ARRAY['owner','clinic_admin']) THEN
         RAISE EXCEPTION 'invalid policy approval' USING ERRCODE = '23514';
       END IF;
     ELSIF OLD.state IN ('proposed','approved') AND NEW.state = 'retired' THEN
       IF (NEW.approved_by_id,NEW.approved_at) IS DISTINCT FROM
          (OLD.approved_by_id,OLD.approved_at)
          OR NEW.retired_by_id <>
            NULLIF(current_setting('app.current_user_id', true), '')::uuid
          OR NEW.retired_at IS NULL
          OR NOT clinic_app.questionnaire_staff(NEW.clinic_id,
            ARRAY['owner','clinic_admin']) THEN
         RAISE EXCEPTION 'invalid policy retirement' USING ERRCODE = '23514';
       END IF;
     ELSE
       RAISE EXCEPTION 'invalid policy transition' USING ERRCODE = '23514';
     END IF;
   END IF;
 ELSIF TG_TABLE_NAME = 'retention_legalhold' THEN
   IF TG_OP = 'INSERT' THEN
     SELECT * INTO scope FROM clinic_app.retention_record_scope(
       NEW.record_class, NEW.record_id);
     IF scope.record_clinic IS NULL
        OR scope.record_clinic <> NEW.clinic_id
        OR scope.record_organization <> NEW.organization_id
        OR btrim(NEW.authority) = '' OR btrim(NEW.reason) = ''
        OR NEW.released_by_id IS NOT NULL OR NEW.released_at IS NOT NULL
        OR NEW.release_authority <> '' OR NEW.release_reason <> ''
        OR NEW.placed_by_id <>
          NULLIF(current_setting('app.current_user_id', true), '')::uuid
        OR NOT clinic_app.questionnaire_staff(NEW.clinic_id,
          ARRAY['owner','clinic_admin']) THEN
       RAISE EXCEPTION 'invalid hold binding' USING ERRCODE = '23514';
     END IF;
   ELSE
     IF (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.record_class,NEW.record_id,
         NEW.authority,NEW.reason,NEW.placed_by_id,NEW.created_at)
        IS DISTINCT FROM
        (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.record_class,OLD.record_id,
         OLD.authority,OLD.reason,OLD.placed_by_id,OLD.created_at)
        OR OLD.released_at IS NOT NULL
        OR NEW.released_at IS NULL
        OR NEW.released_by_id <>
          NULLIF(current_setting('app.current_user_id', true), '')::uuid
        OR btrim(NEW.release_authority) = '' OR btrim(NEW.release_reason) = ''
        OR NOT clinic_app.questionnaire_staff(NEW.clinic_id,
          ARRAY['owner','clinic_admin']) THEN
       RAISE EXCEPTION 'invalid hold release' USING ERRCODE = '23514';
     END IF;
   END IF;
 ELSIF TG_TABLE_NAME = 'retention_recordrelease' THEN
   IF TG_OP = 'INSERT' THEN
     SELECT * INTO version_row FROM clinic_app.ehr_clinicaldocumentversion v
       WHERE v.id = NEW.version_id;
     IF version_row.id IS NOT NULL THEN
       SELECT e.* INTO encounter_row FROM clinic_app.ehr_clinicaldocument d
         JOIN clinic_app.ehr_encounter e ON e.id = d.encounter_id
         WHERE d.id = version_row.document_id;
     END IF;
     IF version_row.id IS NULL OR encounter_row.id IS NULL
        OR version_row.state NOT IN ('finalized','superseded')
        OR encounter_row.clinic_id <> NEW.clinic_id
        OR encounter_row.patient_id <> NEW.patient_id
        OR encounter_row.organization_id <> NEW.organization_id
        OR NEW.released_by_id <> encounter_row.physician_id
        OR NEW.released_by_id <>
          NULLIF(current_setting('app.current_user_id', true), '')::uuid
        OR NEW.revoked_by_id IS NOT NULL OR NEW.revoked_at IS NOT NULL
        OR NOT clinic_app.ehr_assigned(encounter_row.id) THEN
       RAISE EXCEPTION 'invalid release binding' USING ERRCODE = '23514';
     END IF;
   ELSE
     IF (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.patient_id,NEW.version_id,
         NEW.released_by_id,NEW.created_at)
        IS DISTINCT FROM
        (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.patient_id,OLD.version_id,
         OLD.released_by_id,OLD.created_at)
        OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
        OR NEW.revoked_by_id <>
          NULLIF(current_setting('app.current_user_id', true), '')::uuid THEN
       RAISE EXCEPTION 'invalid release revocation' USING ERRCODE = '23514';
     END IF;
     SELECT * INTO version_row FROM clinic_app.ehr_clinicaldocumentversion v
       WHERE v.id = NEW.version_id;
     SELECT e.* INTO encounter_row FROM clinic_app.ehr_clinicaldocument d
       JOIN clinic_app.ehr_encounter e ON e.id = d.encounter_id
       WHERE d.id = version_row.document_id;
     IF NOT clinic_app.ehr_assigned(encounter_row.id) THEN
       RAISE EXCEPTION 'invalid release revocation' USING ERRCODE = '23514';
     END IF;
   END IF;
 ELSIF TG_TABLE_NAME = 'retention_recordexport' THEN
   IF NEW.kind = 'staff' THEN
     IF NEW.requested_by_id IS NULL OR NEW.patient_session_id IS NOT NULL
        OR NEW.requested_by_id <>
          NULLIF(current_setting('app.current_user_id', true), '')::uuid
        OR NOT clinic_app.retention_care(NEW.clinic_id, NEW.patient_id) THEN
       RAISE EXCEPTION 'invalid export binding' USING ERRCODE = '23514';
     END IF;
   ELSIF NEW.kind = 'patient' THEN
     IF NEW.requested_by_id IS NOT NULL OR NEW.patient_session_id IS NULL
        OR NOT EXISTS (SELECT 1
          FROM clinic_app.retention_records_session() s
          WHERE s.session_id = NEW.patient_session_id
          AND s.organization_id = NEW.organization_id
          AND s.clinic_id = NEW.clinic_id
          AND s.patient_id = NEW.patient_id) THEN
       RAISE EXCEPTION 'invalid export binding' USING ERRCODE = '23514';
     END IF;
   ELSE
     RAISE EXCEPTION 'invalid export binding' USING ERRCODE = '23514';
   END IF;
   IF NEW.manifest_digest !~ '^[0-9a-f]{64}$'
      OR NEW.record_count < 0
      OR jsonb_typeof(NEW.manifest) <> 'object'
      OR NEW.manifest->>'export_id' <> NEW.id::text
      OR NEW.manifest->>'manifest_sha256' <> NEW.manifest_digest
      OR NEW.manifest->>'record_count' IS NULL
      OR (NEW.manifest->>'record_count')::integer <> NEW.record_count THEN
     RAISE EXCEPTION 'invalid export manifest' USING ERRCODE = '23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;

CREATE FUNCTION clinic_app.retention_hold_fence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('retention:'||NEW.clinic_id,0));
 IF TG_OP='INSERT' AND EXISTS(SELECT 1 FROM clinic_app.retention_artifactpurge p
  WHERE p.clinic_id=NEW.clinic_id AND p.started_at IS NOT NULL
  AND clinic_app.retention_related_hold(p.record_class,p.record_id,
   NEW.record_class,NEW.record_id)) THEN
  RAISE EXCEPTION 'destruction already started' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_hold_fence() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_hold_fence() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER retention_hold_fence BEFORE INSERT OR UPDATE
 ON clinic_app.retention_legalhold FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_hold_fence();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.retention_hold_fence() FROM clinic_owner;
RESET ROLE;

"""
