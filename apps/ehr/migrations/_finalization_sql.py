"""Lifecycle transitions for finalized versions, amendments and closed encounters.

The binding trigger is the database's independent copy of the contract: it
admits only draft saves, draft->finalized (fixed digest and timestamp),
draft->discarded and finalized->superseded while a linked amendment draft
exists. Encounter UPDATE is limited to the terminal open->closed transition;
DELETE stays forbidden everywhere.
"""

_sql_parts = [
    """
CREATE TABLE clinic_app.ehr_discarded_content (
 version_id uuid PRIMARY KEY,
 organization_id uuid NOT NULL,
 document_id uuid NOT NULL,
 author_id uuid NOT NULL,
 subjective text NOT NULL,
 objective text NOT NULL,
 assessment text NOT NULL,
 plan text NOT NULL,
 archived_at timestamptz NOT NULL DEFAULT statement_timestamp()
);
ALTER TABLE clinic_app.ehr_discarded_content ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.ehr_discarded_content FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.ehr_discarded_content TO clinic_owner
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid)
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid);
REVOKE ALL ON clinic_app.ehr_discarded_content FROM PUBLIC, clinic_app;
GRANT INSERT ON clinic_app.ehr_discarded_content TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.ehr_binding_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 encounter_row clinic_app.ehr_encounter;
 document_row clinic_app.ehr_clinicaldocument;
 base_row clinic_app.ehr_clinicaldocumentversion;
 latest integer;
BEGIN
 IF TG_TABLE_NAME = 'ehr_encounter' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
     JOIN clinic_app.intake_patientclinicenrollment p
       ON p.patient_id = a.patient_id AND p.clinic_id = a.clinic_id
       AND p.organization_id = a.organization_id
     WHERE a.id = NEW.appointment_id AND a.organization_id = NEW.organization_id
       AND a.clinic_id = NEW.clinic_id AND a.patient_id = NEW.patient_id
       AND a.practitioner_id = NEW.physician_id AND a.status = 'scheduled')
     OR NEW.state <> 'open' OR NEW.revision <> 1 THEN
      RAISE EXCEPTION 'invalid encounter binding' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_specialtytemplate' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
      WHERE c.id = NEW.clinic_id AND c.organization_id = NEW.organization_id)
      OR jsonb_typeof(NEW.prompts) <> 'object'
      OR NOT (NEW.prompts ?& ARRAY['subjective','objective','assessment','plan'])
      OR (NEW.prompts - ARRAY['subjective','objective','assessment','plan'])
         <> '{}'::jsonb THEN
     RAISE EXCEPTION 'invalid template binding' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_clinicaldocument' THEN
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = NEW.encounter_id;
   IF encounter_row.id IS NULL
      OR encounter_row.organization_id <> NEW.organization_id
      OR encounter_row.state <> 'open' OR NEW.kind <> 'soap' THEN
     RAISE EXCEPTION 'invalid document binding' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_clinicaldocumentversion' THEN
   SELECT * INTO document_row FROM clinic_app.ehr_clinicaldocument
     WHERE id = NEW.document_id;
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = document_row.encounter_id;
   IF document_row.id IS NULL OR document_row.organization_id <> NEW.organization_id
      OR encounter_row.physician_id <> NEW.author_id
      OR NOT EXISTS (SELECT 1 FROM clinic_app.ehr_specialtytemplate t
         WHERE t.id = NEW.template_id AND t.clinic_id = encounter_row.clinic_id
         AND t.organization_id = NEW.organization_id) THEN
     RAISE EXCEPTION 'invalid version binding' USING ERRCODE = '23514';
   END IF;
   IF TG_OP = 'INSERT' THEN
     -- Serialize draft creation against a concurrent encounter close: the
     -- close guard takes the same transaction-scoped advisory lock while it
     -- holds the encounter row lock, so this wait ends with the committed
     -- encounter state visible to the re-read below.
     PERFORM pg_catalog.pg_advisory_xact_lock(
       hashtextextended('ehr-encounter:' || document_row.encounter_id::text,
                        0));
     SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
       WHERE id = document_row.encounter_id;
     IF NEW.state <> 'draft' OR NEW.revision <> 1
        OR NEW.content_digest <> '' OR NEW.finalized_at IS NOT NULL THEN
       RAISE EXCEPTION 'invalid initial draft' USING ERRCODE = '23514';
     END IF;
     SELECT max(version) INTO latest FROM clinic_app.ehr_clinicaldocumentversion
       WHERE document_id = NEW.document_id;
     IF NEW.version <> coalesce(latest, 0) + 1 THEN
       RAISE EXCEPTION 'invalid version sequence' USING ERRCODE = '23514';
     END IF;
     IF NEW.amendment_of_id IS NULL THEN
       IF encounter_row.state <> 'open'
          OR NEW.subjective <> '' OR NEW.objective <> ''
          OR NEW.assessment <> '' OR NEW.plan <> '' OR NEW.amendment_reason <> ''
          OR EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
            WHERE v.document_id = NEW.document_id
            AND v.state IN ('finalized','superseded')) THEN
         RAISE EXCEPTION 'invalid initial draft' USING ERRCODE = '23514';
       END IF;
     ELSE
       SELECT * INTO base_row FROM clinic_app.ehr_clinicaldocumentversion
         WHERE id = NEW.amendment_of_id;
       IF base_row.id IS NULL OR base_row.document_id <> NEW.document_id
          OR base_row.state <> 'finalized' OR btrim(NEW.amendment_reason) = ''
          OR NEW.template_id <> base_row.template_id
          OR (NEW.subjective,NEW.objective,NEW.assessment,NEW.plan)
             IS DISTINCT FROM
             (base_row.subjective,base_row.objective,base_row.assessment,
              base_row.plan) THEN
         RAISE EXCEPTION 'invalid amendment draft' USING ERRCODE = '23514';
       END IF;
     END IF;
   ELSE
     IF (NEW.id,NEW.organization_id,NEW.document_id,NEW.template_id,NEW.author_id,
         NEW.amendment_of_id,NEW.amendment_reason,NEW.version,NEW.created_at)
        IS DISTINCT FROM
        (OLD.id,OLD.organization_id,OLD.document_id,OLD.template_id,OLD.author_id,
         OLD.amendment_of_id,OLD.amendment_reason,OLD.version,OLD.created_at) THEN
       RAISE EXCEPTION 'immutable version identity' USING ERRCODE = '23514';
     END IF;
     IF OLD.state = 'draft' AND NEW.state = 'draft' THEN
       IF NEW.revision <> OLD.revision + 1
          OR NEW.content_digest <> '' OR NEW.finalized_at IS NOT NULL THEN
         RAISE EXCEPTION 'immutable version or stale revision'
           USING ERRCODE = '23514';
       END IF;
     ELSIF OLD.state = 'draft' AND NEW.state = 'finalized' THEN
       IF NEW.revision <> OLD.revision
          OR (NEW.subjective,NEW.objective,NEW.assessment,NEW.plan)
             IS DISTINCT FROM
             (OLD.subjective,OLD.objective,OLD.assessment,OLD.plan)
          OR btrim(NEW.subjective) = '' OR btrim(NEW.objective) = ''
          OR btrim(NEW.assessment) = '' OR btrim(NEW.plan) = ''
          OR NEW.content_digest !~ '^[0-9a-f]{64}$'
          OR NEW.finalized_at IS NULL THEN
         RAISE EXCEPTION 'invalid finalization' USING ERRCODE = '23514';
       END IF;
     ELSIF OLD.state = 'draft' AND NEW.state = 'discarded' THEN
       IF (NEW.revision,NEW.content_digest,NEW.finalized_at)
          IS DISTINCT FROM
          (OLD.revision,OLD.content_digest,OLD.finalized_at) THEN
         RAISE EXCEPTION 'invalid discard' USING ERRCODE = '23514';
       END IF;
       -- Discarded rows expose metadata only: the SOAP body moves to the
       -- owner-only archive and the stored row is blanked for every caller.
       INSERT INTO clinic_app.ehr_discarded_content
         (version_id,organization_id,document_id,author_id,
          subjective,objective,assessment,plan)
       VALUES (OLD.id,OLD.organization_id,OLD.document_id,OLD.author_id,
               OLD.subjective,OLD.objective,OLD.assessment,OLD.plan);
       NEW.subjective := '';
       NEW.objective := '';
       NEW.assessment := '';
       NEW.plan := '';
     ELSIF OLD.state = 'finalized' AND NEW.state = 'superseded' THEN
       IF (NEW.subjective,NEW.objective,NEW.assessment,NEW.plan,NEW.revision,
           NEW.content_digest,NEW.finalized_at)
          IS DISTINCT FROM
          (OLD.subjective,OLD.objective,OLD.assessment,OLD.plan,OLD.revision,
           OLD.content_digest,OLD.finalized_at)
          OR NOT EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
            WHERE v.document_id = OLD.document_id AND v.state = 'draft'
            AND v.amendment_of_id = OLD.id) THEN
         RAISE EXCEPTION 'invalid supersede' USING ERRCODE = '23514';
       END IF;
     ELSE
       RAISE EXCEPTION 'immutable version or stale revision'
         USING ERRCODE = '23514';
     END IF;
   END IF;
   IF greatest(length(NEW.subjective),length(NEW.objective),
      length(NEW.assessment),length(NEW.plan)) > 20000 THEN
     RAISE EXCEPTION 'SOAP content limit' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_encounterintakereference' THEN
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = NEW.encounter_id;
   IF encounter_row.id IS NULL
      OR encounter_row.organization_id <> NEW.organization_id
      OR NOT EXISTS (SELECT 1 FROM clinic_app.intake_questionnaireevent q
       JOIN clinic_app.intake_questionnaireresponse r ON r.id = q.response_id
       WHERE q.id = NEW.submission_id AND q.action = 'submitted'
       AND q.organization_id = NEW.organization_id
       AND q.clinic_id = encounter_row.clinic_id
       AND r.patient_id = encounter_row.patient_id
       AND (r.appointment_id IS NULL
         OR r.appointment_id = encounter_row.appointment_id)) THEN
     RAISE EXCEPTION 'invalid intake reference' USING ERRCODE = '23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.ehr_next_version(requested_document uuid)
RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT coalesce(max(version), 0) + 1 FROM clinic_app.ehr_clinicaldocumentversion
 WHERE document_id = requested_document
$f$;
CREATE FUNCTION clinic_app.ehr_encounter_close_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.appointment_id,NEW.patient_id,
     NEW.physician_id,NEW.revision,NEW.created_at)
    IS DISTINCT FROM
    (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.appointment_id,OLD.patient_id,
     OLD.physician_id,OLD.revision,OLD.created_at)
    OR OLD.state <> 'open' OR NEW.state <> 'closed'
    OR NEW.closed_at IS NULL THEN
   RAISE EXCEPTION 'invalid encounter transition' USING ERRCODE = '23514';
 END IF;
 -- Serialize against in-flight version inserts: they take the same
 -- transaction-scoped advisory lock before checking this encounter's state,
 -- so a live draft here is a real contract violation, not a stale read.
 PERFORM pg_catalog.pg_advisory_xact_lock(
   hashtextextended('ehr-encounter:' || OLD.id::text, 0));
 IF EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
   JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
   WHERE d.encounter_id = OLD.id AND v.state = 'draft') THEN
   RAISE EXCEPTION 'draft_in_progress' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_next_version(uuid),
 clinic_app.ehr_encounter_close_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_next_version(uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_encounter_close_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
""",
    """
DROP TRIGGER ehr_immutable ON clinic_app.ehr_encounter;
CREATE TRIGGER ehr_close BEFORE UPDATE ON clinic_app.ehr_encounter
 FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_encounter_close_guard();
CREATE TRIGGER ehr_immutable BEFORE DELETE ON clinic_app.ehr_encounter
 FOR EACH ROW EXECUTE FUNCTION clinic_app.questionnaire_immutable();
DROP POLICY clinical_read ON clinic_app.ehr_clinicaldocumentversion;
CREATE POLICY clinical_read ON clinic_app.ehr_clinicaldocumentversion
 FOR SELECT TO clinic_app
 USING ((state <> 'discarded' AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_clinicaldocument d WHERE d.id = document_id
 AND d.organization_id = ehr_clinicaldocumentversion.organization_id
 AND ((clinic_app.ehr_assigned(d.encounter_id) AND
   (state <> 'draft' OR author_id =
     NULLIF(current_setting('app.current_user_id', true), '')::uuid))
 OR (state IN ('finalized','superseded') AND clinic_app.ehr_care(d.encounter_id)))))
 OR (state = 'discarded' AND author_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d WHERE d.id = document_id
 AND clinic_app.ehr_assigned(d.encounter_id))));
DROP POLICY clinical_update ON clinic_app.ehr_clinicaldocumentversion;
CREATE POLICY clinical_update ON clinic_app.ehr_clinicaldocumentversion
 FOR UPDATE TO clinic_app
 USING (EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d
   WHERE d.id = document_id AND clinic_app.ehr_assigned(d.encounter_id))
  AND ((state = 'draft' AND author_id =
    NULLIF(current_setting('app.current_user_id', true), '')::uuid)
   OR state = 'finalized'))
 WITH CHECK (EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d
   WHERE d.id = document_id AND clinic_app.ehr_assigned(d.encounter_id))
  AND ((state IN ('draft','discarded') AND author_id =
    NULLIF(current_setting('app.current_user_id', true), '')::uuid)
   OR state IN ('finalized','superseded')));
GRANT UPDATE (subjective,objective,assessment,plan,revision,updated_at,state,
 content_digest,finalized_at) ON clinic_app.ehr_clinicaldocumentversion
 TO clinic_app;
GRANT UPDATE (revision,state,closed_at) ON clinic_app.ehr_encounter TO clinic_app;
""",
    """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ehr_encounter_close_guard(),
 clinic_app.questionnaire_immutable() FROM clinic_owner;
RESET ROLE;
""",
]
SQL = "".join(_sql_parts)

REVERSE_SQL = """
DROP TRIGGER ehr_close ON clinic_app.ehr_encounter;
DROP TRIGGER ehr_immutable ON clinic_app.ehr_encounter;
DROP POLICY clinical_read ON clinic_app.ehr_clinicaldocumentversion;
CREATE POLICY clinical_read ON clinic_app.ehr_clinicaldocumentversion
 FOR SELECT TO clinic_app
 USING (state <> 'discarded' AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_clinicaldocument d WHERE d.id = document_id
 AND d.organization_id = ehr_clinicaldocumentversion.organization_id
 AND ((clinic_app.ehr_assigned(d.encounter_id) AND
   (state <> 'draft' OR author_id =
     NULLIF(current_setting('app.current_user_id', true), '')::uuid))
 OR (state IN ('finalized','superseded') AND clinic_app.ehr_care(d.encounter_id)))));
DROP POLICY clinical_update ON clinic_app.ehr_clinicaldocumentversion;
REVOKE UPDATE ON clinic_app.ehr_clinicaldocumentversion FROM clinic_app;
REVOKE UPDATE ON clinic_app.ehr_encounter FROM clinic_app;
SET LOCAL ROLE clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
DROP TABLE clinic_app.ehr_discarded_content;
CREATE TRIGGER ehr_immutable BEFORE UPDATE OR DELETE ON clinic_app.ehr_encounter
 FOR EACH ROW EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE POLICY clinical_update ON clinic_app.ehr_clinicaldocumentversion
 FOR UPDATE TO clinic_app
 USING (state = 'draft' AND author_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d WHERE d.id = document_id
 AND clinic_app.ehr_assigned(d.encounter_id)))
 WITH CHECK (state = 'draft' AND author_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d WHERE d.id = document_id
 AND clinic_app.ehr_assigned(d.encounter_id)));
GRANT UPDATE (subjective,objective,assessment,plan,revision,updated_at)
 ON clinic_app.ehr_clinicaldocumentversion TO clinic_app;
GRANT UPDATE (revision) ON clinic_app.ehr_encounter TO clinic_app;
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.questionnaire_immutable() FROM clinic_owner;
CREATE OR REPLACE FUNCTION clinic_app.ehr_binding_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 encounter_row clinic_app.ehr_encounter;
 document_row clinic_app.ehr_clinicaldocument;
BEGIN
 IF TG_TABLE_NAME = 'ehr_encounter' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
     JOIN clinic_app.intake_patientclinicenrollment p
       ON p.patient_id = a.patient_id AND p.clinic_id = a.clinic_id
       AND p.organization_id = a.organization_id
     WHERE a.id = NEW.appointment_id AND a.organization_id = NEW.organization_id
       AND a.clinic_id = NEW.clinic_id AND a.patient_id = NEW.patient_id
       AND a.practitioner_id = NEW.physician_id AND a.status = 'scheduled')
     OR NEW.state <> 'open' OR NEW.revision <> 1 THEN
      RAISE EXCEPTION 'invalid encounter binding' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_specialtytemplate' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
      WHERE c.id = NEW.clinic_id AND c.organization_id = NEW.organization_id)
      OR jsonb_typeof(NEW.prompts) <> 'object'
      OR NOT (NEW.prompts ?& ARRAY['subjective','objective','assessment','plan'])
      OR (NEW.prompts - ARRAY['subjective','objective','assessment','plan'])
         <> '{}'::jsonb THEN
     RAISE EXCEPTION 'invalid template binding' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_clinicaldocument' THEN
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = NEW.encounter_id;
   IF encounter_row.id IS NULL
      OR encounter_row.organization_id <> NEW.organization_id
      OR encounter_row.state <> 'open' OR NEW.kind <> 'soap' THEN
     RAISE EXCEPTION 'invalid document binding' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_clinicaldocumentversion' THEN
   SELECT * INTO document_row FROM clinic_app.ehr_clinicaldocument
     WHERE id = NEW.document_id;
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = document_row.encounter_id;
   IF document_row.id IS NULL OR document_row.organization_id <> NEW.organization_id
      OR encounter_row.physician_id <> NEW.author_id
      OR NOT EXISTS (SELECT 1 FROM clinic_app.ehr_specialtytemplate t
         WHERE t.id = NEW.template_id AND t.clinic_id = encounter_row.clinic_id
         AND t.organization_id = NEW.organization_id) THEN
     RAISE EXCEPTION 'invalid version binding' USING ERRCODE = '23514';
   END IF;
   IF TG_OP = 'INSERT' THEN
     IF encounter_row.state <> 'open' OR NEW.state <> 'draft' OR NEW.version <> 1
        OR NEW.revision <> 1 OR NEW.subjective <> '' OR NEW.objective <> ''
        OR NEW.assessment <> '' OR NEW.plan <> '' THEN
       RAISE EXCEPTION 'invalid initial draft' USING ERRCODE = '23514';
     END IF;
   ELSE
     IF (NEW.id,NEW.organization_id,NEW.document_id,NEW.template_id,NEW.author_id,
         NEW.version,NEW.state,NEW.created_at) IS DISTINCT FROM
        (OLD.id,OLD.organization_id,OLD.document_id,OLD.template_id,OLD.author_id,
         OLD.version,OLD.state,OLD.created_at)
        OR OLD.state <> 'draft' OR NEW.revision <> OLD.revision + 1 THEN
       RAISE EXCEPTION 'immutable version or stale revision' USING ERRCODE = '23514';
     END IF;
   END IF;
   IF greatest(length(NEW.subjective),length(NEW.objective),
      length(NEW.assessment),length(NEW.plan)) > 20000 THEN
     RAISE EXCEPTION 'SOAP content limit' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_encounterintakereference' THEN
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = NEW.encounter_id;
   IF encounter_row.id IS NULL
      OR encounter_row.organization_id <> NEW.organization_id
      OR NOT EXISTS (SELECT 1 FROM clinic_app.intake_questionnaireevent q
       JOIN clinic_app.intake_questionnaireresponse r ON r.id = q.response_id
       WHERE q.id = NEW.submission_id AND q.action = 'submitted'
       AND q.organization_id = NEW.organization_id
       AND q.clinic_id = encounter_row.clinic_id
       AND r.patient_id = encounter_row.patient_id
       AND (r.appointment_id IS NULL
         OR r.appointment_id = encounter_row.appointment_id)) THEN
     RAISE EXCEPTION 'invalid intake reference' USING ERRCODE = '23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
DROP FUNCTION clinic_app.ehr_next_version(uuid);
DROP FUNCTION clinic_app.ehr_encounter_close_guard();
RESET ROLE;
"""
