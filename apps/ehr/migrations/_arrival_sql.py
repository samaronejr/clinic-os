"""Encounter binding follows appointment lifecycle v2 (todo 22, D-9, H-11).

A scheduled-bound encounter opens only once the patient has arrived (or the
visit is already in progress). Applied by scheduling migration 0006 in the same
transaction as the status widening. The body is the live 0009 guard verbatim,
differing only in the appointment-status predicate.
"""

_BINDING_GUARD = """
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
       AND a.practitioner_id = NEW.physician_id AND a.status __ENCOUNTER_STATUSES__)
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
          OR NEW.content IS NOT NULL OR NEW.content_sha256 <> ''
          OR NEW.amendment_reason IS NOT NULL
          OR EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
            WHERE v.document_id = NEW.document_id
            AND v.state IN ('finalized','superseded')) THEN
         RAISE EXCEPTION 'invalid initial draft' USING ERRCODE = '23514';
       END IF;
     ELSE
       SELECT * INTO base_row FROM clinic_app.ehr_clinicaldocumentversion
         WHERE id = NEW.amendment_of_id;
       -- The amendment draft must carry the base version's exact content:
       -- ciphertext bytes differ across envelopes, so the stored plaintext
       -- digest is the equality proof.
       IF base_row.id IS NULL OR base_row.document_id <> NEW.document_id
          OR base_row.state <> 'finalized' OR NEW.amendment_reason IS NULL
          OR NEW.template_id <> base_row.template_id
          OR NEW.content IS NULL
          OR NEW.content_sha256 IS DISTINCT FROM base_row.content_sha256 THEN
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
          OR NEW.content_digest <> '' OR NEW.finalized_at IS NOT NULL
          OR NEW.content IS NULL
          OR NEW.content_sha256 !~ '^[0-9a-f]{64}$'
          OR (NEW.content IS DISTINCT FROM OLD.content)
             <> (NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256) THEN
         RAISE EXCEPTION 'immutable version or stale revision'
           USING ERRCODE = '23514';
       END IF;
     ELSIF OLD.state = 'draft' AND NEW.state = 'finalized' THEN
       IF NEW.revision <> OLD.revision
          OR (NEW.content, NEW.content_sha256)
             IS DISTINCT FROM (OLD.content, OLD.content_sha256)
          OR NEW.content IS NULL
          OR NEW.content_sha256 !~ '^[0-9a-f]{64}$'
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
       -- Discarded rows expose metadata only: the encrypted SOAP body moves
       -- to the owner-only archive and the stored row loses its envelope.
       INSERT INTO clinic_app.ehr_discarded_content
         (version_id,organization_id,document_id,author_id,
          content,content_sha256)
       VALUES (OLD.id,OLD.organization_id,OLD.document_id,OLD.author_id,
               OLD.content,OLD.content_sha256);
       NEW.content := NULL;
       NEW.content_sha256 := '';
     ELSIF OLD.state = 'finalized' AND NEW.state = 'superseded' THEN
       IF (NEW.content,NEW.content_sha256,NEW.revision,
           NEW.content_digest,NEW.finalized_at)
          IS DISTINCT FROM
          (OLD.content,OLD.content_sha256,OLD.revision,
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
   IF NEW.content IS NOT NULL
      AND pg_catalog.octet_length(NEW.content) > 120000 THEN
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
"""

SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _BINDING_GUARD.replace(
        "__ENCOUNTER_STATUSES__", "IN ('arrived', 'in_progress')", 1
    )
    + "RESET ROLE;\n"
)

REVERSE_SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _BINDING_GUARD.replace("__ENCOUNTER_STATUSES__", "= 'scheduled'", 1)
    + "RESET ROLE;\n"
)
