"""Retention predicates, lifecycle bindings and least-privilege grants.

Resolver functions run as ``clinic_resolver`` (BYPASSRLS) but every staff
predicate re-checks the tenant GUC and canonical role table, and every
patient predicate re-validates the live session row. Trigger guards make
identity, binding and content immutable and admit only the lifecycle
transitions the services perform; DELETE is forbidden on every table.
"""

TABLES = (
    "retentionpolicy",
    "legalhold",
    "recordrelease",
    "recordexport",
)

_sql_parts = [
    """
GRANT SELECT ON clinic_app.ehr_encounter, clinic_app.ehr_clinicaldocument,
 clinic_app.ehr_clinicaldocumentversion, clinic_app.ehr_clinicalattachment,
 clinic_app.ehr_historyassessment, clinic_app.intake_patient,
 clinic_app.intake_patientsession, clinic_app.intake_patientaccessgrant,
 clinic_app.retention_retentionpolicy, clinic_app.retention_legalhold,
 clinic_app.retention_recordrelease, clinic_app.retention_recordexport
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.retention_record_scope(
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
 IF requested_class = 'ehr.encounter' THEN
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
CREATE FUNCTION clinic_app.retention_care(
 requested_clinic pg_catalog.uuid, requested_patient pg_catalog.uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment n
   WHERE n.clinic_id = requested_clinic AND n.patient_id = requested_patient
   AND n.organization_id =
     NULLIF(current_setting('app.current_tenant', true), '')::uuid
   AND clinic_app.questionnaire_staff(requested_clinic, ARRAY['physician'])
   AND (EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
     WHERE a.clinic_id = requested_clinic AND a.patient_id = requested_patient
     AND a.practitioner_id =
       NULLIF(current_setting('app.current_user_id', true), '')::uuid)
   OR EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
     JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
     JOIN clinic_app.ehr_encounter e ON e.id = d.encounter_id
     WHERE e.clinic_id = requested_clinic AND e.patient_id = requested_patient
     AND v.author_id =
       NULLIF(current_setting('app.current_user_id', true), '')::uuid)))
$f$;
CREATE FUNCTION clinic_app.retention_records_session()
RETURNS TABLE(session_id pg_catalog.uuid, organization_id pg_catalog.uuid,
              clinic_id pg_catalog.uuid, patient_id pg_catalog.uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT s.id, s.organization_id, s.clinic_id, s.patient_id
   FROM clinic_app.intake_patientsession s
   JOIN clinic_app.intake_patientaccessgrant g ON g.id = s.grant_id
   WHERE s.id =
     NULLIF(current_setting('app.current_patient_session', true), '')::uuid
   AND s.revoked_at IS NULL AND g.revoked_at IS NULL
   AND s.expires_at > statement_timestamp()
   AND s.idle_expires_at > statement_timestamp()
   AND 'records' = ANY(s.operations)
$f$;
CREATE FUNCTION clinic_app.retention_patient_releases()
RETURNS TABLE(release_id pg_catalog.uuid, version_id pg_catalog.uuid,
              document_id pg_catalog.uuid, encounter_id pg_catalog.uuid,
              version_number pg_catalog.int4,
              state pg_catalog.text, content_digest pg_catalog.text,
              finalized_at pg_catalog.timestamptz,
              amendment_of_version pg_catalog.int4,
              author_label pg_catalog.text,
              subjective pg_catalog.text, objective pg_catalog.text,
              assessment pg_catalog.text, plan pg_catalog.text,
              released_at pg_catalog.timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT r.id, v.id, d.id, e.id, v.version, v.state::pg_catalog.text,
   v.content_digest::pg_catalog.text,
   v.finalized_at, base.version, u.username::pg_catalog.text,
   v.subjective, v.objective, v.assessment, v.plan, r.created_at
   FROM clinic_app.retention_recordrelease r
   JOIN clinic_app.ehr_clinicaldocumentversion v ON v.id = r.version_id
   JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
   JOIN clinic_app.ehr_encounter e ON e.id = d.encounter_id
   JOIN clinic_app.identity_user u ON u.id = v.author_id
   LEFT JOIN clinic_app.ehr_clinicaldocumentversion base
     ON base.id = v.amendment_of_id
   WHERE r.revoked_at IS NULL
   AND EXISTS (SELECT 1 FROM clinic_app.retention_records_session() s
     WHERE s.organization_id = r.organization_id
     AND s.clinic_id = r.clinic_id AND s.patient_id = r.patient_id)
   ORDER BY e.created_at, d.id, v.version
$f$;
CREATE FUNCTION clinic_app.retention_care_patients(requested_clinic pg_catalog.uuid)
RETURNS TABLE(patient_id pg_catalog.uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT DISTINCT r.patient_id FROM clinic_app.retention_recordrelease r
   WHERE r.clinic_id = requested_clinic AND r.revoked_at IS NULL
   AND r.organization_id =
     NULLIF(current_setting('app.current_tenant', true), '')::uuid
   AND clinic_app.retention_care(requested_clinic, r.patient_id)
$f$;
CREATE FUNCTION clinic_app.retention_author_label(requested_user pg_catalog.uuid)
RETURNS pg_catalog.text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 -- Only an author of a clinical version in a clinic where the caller is
 -- staff resolves; every other id is indistinguishable from unknown.
 SELECT u.username::pg_catalog.text FROM clinic_app.identity_user u
   WHERE u.id = requested_user
   AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
     JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
     JOIN clinic_app.ehr_encounter e ON e.id = d.encounter_id
     WHERE v.author_id = u.id
     AND e.organization_id =
       NULLIF(current_setting('app.current_tenant', true), '')::uuid
     AND clinic_app.questionnaire_staff(e.clinic_id,
       ARRAY['physician','receptionist','owner','clinic_admin']))
$f$;
CREATE FUNCTION clinic_app.retention_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 scope RECORD;
 encounter_row clinic_app.ehr_encounter;
 version_row clinic_app.ehr_clinicaldocumentversion;
 latest integer;
BEGIN
 IF TG_TABLE_NAME = 'retention_retentionpolicy' THEN
   IF TG_OP = 'INSERT' THEN
     IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
        WHERE c.id = NEW.clinic_id AND c.organization_id = NEW.organization_id)
        OR NEW.record_class NOT IN ('ehr.encounter','ehr.document_version',
          'ehr.clinical_attachment','ehr.history_assessment','ehr.problem',
          'ehr.allergy')
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
CREATE FUNCTION clinic_app.retention_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 RAISE EXCEPTION 'retention records are immutable' USING ERRCODE = '23514';
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.retention_record_scope(text, uuid),
 clinic_app.retention_care(uuid, uuid),
 clinic_app.retention_care_patients(uuid),
 clinic_app.retention_records_session(),
 clinic_app.retention_patient_releases(),
 clinic_app.retention_author_label(uuid), clinic_app.retention_guard(),
 clinic_app.retention_immutable() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_record_scope(text, uuid),
 clinic_app.retention_care(uuid, uuid),
 clinic_app.retention_care_patients(uuid),
 clinic_app.retention_records_session(),
 clinic_app.retention_patient_releases(),
 clinic_app.retention_author_label(uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.retention_guard(),
 clinic_app.retention_immutable() TO clinic_owner;
RESET ROLE;
""",
]

_sql_parts.append("""
ALTER TABLE clinic_app.retention_retentionpolicy ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.retention_retentionpolicy FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.retention_retentionpolicy
 TO clinic_owner
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid)
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid);
REVOKE ALL ON clinic_app.retention_retentionpolicy FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.retention_retentionpolicy TO clinic_app;
GRANT UPDATE (state, approved_by_id, approved_at, retired_by_id, retired_at)
 ON clinic_app.retention_retentionpolicy TO clinic_app;
CREATE TRIGGER retention_policy_binding BEFORE INSERT OR UPDATE
 ON clinic_app.retention_retentionpolicy FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_guard();
CREATE TRIGGER retention_policy_immutable BEFORE DELETE
 ON clinic_app.retention_retentionpolicy FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_immutable();
CREATE POLICY policy_read ON clinic_app.retention_retentionpolicy
 FOR SELECT TO clinic_app
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,
   ARRAY['physician','receptionist','owner','clinic_admin']));
CREATE POLICY policy_insert ON clinic_app.retention_retentionpolicy
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND proposed_by_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['owner','clinic_admin']));
CREATE POLICY policy_update ON clinic_app.retention_retentionpolicy
 FOR UPDATE TO clinic_app
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['owner','clinic_admin']))
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND state IN ('approved','retired')
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['owner','clinic_admin']));

ALTER TABLE clinic_app.retention_legalhold ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.retention_legalhold FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.retention_legalhold TO clinic_owner
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid)
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid);
REVOKE ALL ON clinic_app.retention_legalhold FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.retention_legalhold TO clinic_app;
GRANT UPDATE (released_by_id, released_at, release_authority, release_reason)
 ON clinic_app.retention_legalhold TO clinic_app;
CREATE TRIGGER retention_hold_binding BEFORE INSERT OR UPDATE
 ON clinic_app.retention_legalhold FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_guard();
CREATE TRIGGER retention_hold_immutable BEFORE DELETE
 ON clinic_app.retention_legalhold FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_immutable();
CREATE POLICY hold_read ON clinic_app.retention_legalhold
 FOR SELECT TO clinic_app
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,
   ARRAY['physician','receptionist','owner','clinic_admin']));
CREATE POLICY hold_insert ON clinic_app.retention_legalhold
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND placed_by_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['owner','clinic_admin']));
CREATE POLICY hold_release ON clinic_app.retention_legalhold
 FOR UPDATE TO clinic_app
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['owner','clinic_admin']))
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND released_at IS NOT NULL
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['owner','clinic_admin']));

ALTER TABLE clinic_app.retention_recordrelease ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.retention_recordrelease FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.retention_recordrelease TO clinic_owner
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid)
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid);
REVOKE ALL ON clinic_app.retention_recordrelease FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.retention_recordrelease TO clinic_app;
GRANT UPDATE (revoked_by_id, revoked_at)
 ON clinic_app.retention_recordrelease TO clinic_app;
CREATE TRIGGER retention_release_binding BEFORE INSERT OR UPDATE
 ON clinic_app.retention_recordrelease FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_guard();
CREATE TRIGGER retention_release_immutable BEFORE DELETE
 ON clinic_app.retention_recordrelease FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_immutable();
CREATE POLICY release_read ON clinic_app.retention_recordrelease
 FOR SELECT TO clinic_app
 USING ((organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,
   ARRAY['physician','receptionist','owner','clinic_admin']))
 OR EXISTS (SELECT 1 FROM clinic_app.retention_records_session() s
   WHERE s.organization_id = retention_recordrelease.organization_id
   AND s.clinic_id = retention_recordrelease.clinic_id
   AND s.patient_id = retention_recordrelease.patient_id));
CREATE POLICY release_insert ON clinic_app.retention_recordrelease
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND released_by_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
   JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
   WHERE v.id = version_id AND clinic_app.ehr_assigned(d.encounter_id)));
CREATE POLICY release_revoke ON clinic_app.retention_recordrelease
 FOR UPDATE TO clinic_app
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
   JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
   WHERE v.id = version_id AND clinic_app.ehr_assigned(d.encounter_id)))
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND revoked_at IS NOT NULL
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
   JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
   WHERE v.id = version_id AND clinic_app.ehr_assigned(d.encounter_id)));

ALTER TABLE clinic_app.retention_recordexport ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.retention_recordexport FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.retention_recordexport TO clinic_owner
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid)
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid);
REVOKE ALL ON clinic_app.retention_recordexport FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.retention_recordexport TO clinic_app;
CREATE TRIGGER retention_export_binding BEFORE INSERT
 ON clinic_app.retention_recordexport FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_guard();
CREATE TRIGGER retention_export_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.retention_recordexport FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_immutable();
CREATE POLICY export_read ON clinic_app.retention_recordexport
 FOR SELECT TO clinic_app
 USING ((organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,
   ARRAY['physician','receptionist','owner','clinic_admin']))
 OR EXISTS (SELECT 1 FROM clinic_app.retention_records_session() s
   WHERE s.organization_id = retention_recordexport.organization_id
   AND s.clinic_id = retention_recordexport.clinic_id
   AND s.patient_id = retention_recordexport.patient_id));
CREATE POLICY export_insert_staff ON clinic_app.retention_recordexport
 FOR INSERT TO clinic_app
 WITH CHECK (kind = 'staff' AND organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND requested_by_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND clinic_app.retention_care(clinic_id, patient_id));
CREATE POLICY export_insert_patient ON clinic_app.retention_recordexport
 FOR INSERT TO clinic_app
 WITH CHECK (kind = 'patient' AND requested_by_id IS NULL
 AND EXISTS (SELECT 1 FROM clinic_app.retention_records_session() s
   WHERE s.session_id = patient_session_id
   AND s.organization_id = retention_recordexport.organization_id
   AND s.clinic_id = retention_recordexport.clinic_id
   AND s.patient_id = retention_recordexport.patient_id));
""")

_sql_parts.append("""
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.retention_guard(),
 clinic_app.retention_immutable() FROM clinic_owner;
RESET ROLE;
""")

_sql_parts.append("""
-- The patient read audit append stays owner-owned like every audit writer:
-- clinic_app can only ask for the fixed ehr.record.viewed event, and the
-- function itself re-validates the live records session and the release
-- binding before chaining the row. The session id fills actor_user_id so
-- the actor-scope check holds without impersonating any staff user.
CREATE FUNCTION clinic_app.retention_record_viewed(
 requested_version pg_catalog.uuid,
 occurred_at_utc pg_catalog.timestamptz,
 content_hash pg_catalog.bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 session_row RECORD;
 previous_hash pg_catalog.bytea;
 current_hash pg_catalog.bytea;
 captured_clock pg_catalog.timestamptz;
 inserted_seq bigint;
BEGIN
 IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
   RAISE EXCEPTION 'audit append requires read committed isolation'
     USING ERRCODE = '22023';
 END IF;
 SELECT * INTO session_row FROM clinic_app.retention_records_session();
 IF session_row.session_id IS NULL THEN
   RAISE EXCEPTION 'patient records session required' USING ERRCODE = '42501';
 END IF;
 -- The session-scoped resolver is the only release authority here: the
 -- owner role cannot read release rows under FORCE RLS without a tenant.
 IF NOT EXISTS (SELECT 1 FROM clinic_app.retention_patient_releases() p
   WHERE p.version_id = requested_version) THEN
   RAISE EXCEPTION 'version not released to this session' USING ERRCODE = '42501';
 END IF;
 IF content_hash IS NULL OR pg_catalog.octet_length(content_hash) <> 32 THEN
   RAISE EXCEPTION 'content hash must be 32 bytes' USING ERRCODE = '22023';
 END IF;
 PERFORM pg_catalog.pg_advisory_xact_lock(
   pg_catalog.hashtextextended(
     ('clinic-audit:' || session_row.organization_id::pg_catalog.text)
       COLLATE pg_catalog."C", 0::bigint));
 captured_clock := pg_catalog.clock_timestamp();
 IF occurred_at_utc IS NULL
    OR occurred_at_utc < captured_clock - pg_catalog.interval '5 minutes'
    OR occurred_at_utc > captured_clock + pg_catalog.interval '5 minutes' THEN
   RAISE EXCEPTION 'occurrence timestamp is outside the five-minute window'
     USING ERRCODE = '22023';
 END IF;
 SELECT audit_event.curr_hash INTO previous_hash
   FROM clinic_app.audit_event AS audit_event
   WHERE audit_event.organization_id = session_row.organization_id
   ORDER BY audit_event.seq DESC LIMIT 1;
 IF previous_hash IS NULL THEN
   previous_hash := pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex');
 END IF;
 current_hash := clinic_app.digest(content_hash || previous_hash, 'sha256');
 INSERT INTO clinic_app.audit_event (
   organization_id, actor_user_id, event_type, component_id, component_ip,
   affected_record_type, affected_record_id, occurred_at_utc, payload,
   prev_hash, curr_hash)
 VALUES (
   session_row.organization_id, session_row.session_id, 'ehr.record.viewed',
   'clinic-os-web', NULL, 'ehr.document_version', requested_version::text,
   occurred_at_utc,
   pg_catalog.jsonb_build_object('clinic_id', session_row.clinic_id::text,
     'object_verb', 'viewed'),
   previous_hash, current_hash)
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END
$f$;
ALTER FUNCTION clinic_app.retention_record_viewed(
 uuid, timestamptz, bytea) OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.retention_record_viewed(
 uuid, timestamptz, bytea) FROM PUBLIC, clinic_app, clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.retention_record_viewed(
 uuid, timestamptz, bytea) TO clinic_app;
SET LOCAL ROLE clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.retention_records_session(),
 clinic_app.retention_patient_releases() TO clinic_owner;
RESET ROLE;
""")
SQL = "".join(_sql_parts)

REVERSE_SQL = """
DROP TRIGGER retention_policy_binding ON clinic_app.retention_retentionpolicy;
DROP TRIGGER retention_policy_immutable ON clinic_app.retention_retentionpolicy;
DROP TRIGGER retention_hold_binding ON clinic_app.retention_legalhold;
DROP TRIGGER retention_hold_immutable ON clinic_app.retention_legalhold;
DROP TRIGGER retention_release_binding ON clinic_app.retention_recordrelease;
DROP TRIGGER retention_release_immutable ON clinic_app.retention_recordrelease;
DROP TRIGGER retention_export_binding ON clinic_app.retention_recordexport;
DROP TRIGGER retention_export_immutable ON clinic_app.retention_recordexport;
DROP POLICY policy_read ON clinic_app.retention_retentionpolicy;
DROP POLICY policy_insert ON clinic_app.retention_retentionpolicy;
DROP POLICY policy_update ON clinic_app.retention_retentionpolicy;
DROP POLICY hold_read ON clinic_app.retention_legalhold;
DROP POLICY hold_insert ON clinic_app.retention_legalhold;
DROP POLICY hold_release ON clinic_app.retention_legalhold;
DROP POLICY release_read ON clinic_app.retention_recordrelease;
DROP POLICY release_insert ON clinic_app.retention_recordrelease;
DROP POLICY release_revoke ON clinic_app.retention_recordrelease;
DROP POLICY export_read ON clinic_app.retention_recordexport;
DROP POLICY export_insert_staff ON clinic_app.retention_recordexport;
DROP POLICY export_insert_patient ON clinic_app.retention_recordexport;
DROP POLICY setup_tenant ON clinic_app.retention_retentionpolicy;
DROP POLICY setup_tenant ON clinic_app.retention_legalhold;
DROP POLICY setup_tenant ON clinic_app.retention_recordrelease;
DROP POLICY setup_tenant ON clinic_app.retention_recordexport;
DROP FUNCTION clinic_app.retention_record_viewed(uuid, timestamptz, bytea);
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.retention_records_session(),
 clinic_app.retention_patient_releases() FROM clinic_owner;
DROP FUNCTION clinic_app.retention_guard();
DROP FUNCTION clinic_app.retention_immutable();
DROP FUNCTION clinic_app.retention_author_label(uuid);
DROP FUNCTION clinic_app.retention_care_patients(uuid);
DROP FUNCTION clinic_app.retention_patient_releases();
DROP FUNCTION clinic_app.retention_records_session();
DROP FUNCTION clinic_app.retention_care(uuid, uuid);
DROP FUNCTION clinic_app.retention_record_scope(text, uuid);
RESET ROLE;
"""
