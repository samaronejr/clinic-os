"""FORCE RLS, resolvers and guards for verification, release and delivery.

The public verification resolver runs as ``clinic_resolver`` (BYPASSRLS)
and returns only a fixed minimal projection: status, document version,
digests, issuer/clinic labels and the recorded issuance time. Signed
bytes never leave the resolver boundary: the signature itself is
verified inside the privileged function, which publishes only the
verdict. Patient resolvers re-validate the live session row on every
call, exactly like the retention records boundary. The probe table is
reachable only by the resolver-owned allowance function; ``clinic_app``
holds no grant on it.
"""

_parts = [
    """
GRANT SELECT ON clinic_app.prescription_prescriptiondocument,
 clinic_app.prescription_signatureoperation,
 clinic_app.prescription_prescriptiondocumentrelease,
 clinic_app.prescription_prescriptiondocumentrevocation
 TO clinic_resolver;
GRANT SELECT, INSERT, UPDATE, DELETE
 ON clinic_app.prescription_verificationprobe TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.prescription_verify_allowance(
 requested_probe pg_catalog.bytea,
 max_lookups pg_catalog.int4,
 window_seconds pg_catalog.int4
)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE probe_count pg_catalog.int4;
BEGIN
 IF requested_probe IS NULL
    OR pg_catalog.octet_length(requested_probe) <> 32
    OR max_lookups IS NULL OR max_lookups < 1
    OR window_seconds IS NULL OR window_seconds < 1 THEN
   RETURN false;
 END IF;
 INSERT INTO clinic_app.prescription_verificationprobe
  (probe_key, window_start, lookups)
 VALUES (requested_probe, statement_timestamp(), 1)
 ON CONFLICT (probe_key) DO UPDATE SET
   window_start = CASE
     WHEN prescription_verificationprobe.window_start
          + make_interval(secs => window_seconds)
          <= statement_timestamp()
     THEN statement_timestamp()
     ELSE prescription_verificationprobe.window_start END,
   lookups = CASE
     WHEN prescription_verificationprobe.window_start
          + make_interval(secs => window_seconds)
          <= statement_timestamp()
     THEN 1
     ELSE prescription_verificationprobe.lookups + 1 END
 RETURNING lookups INTO probe_count;
 DELETE FROM clinic_app.prescription_verificationprobe
  WHERE window_start < statement_timestamp()
    - make_interval(secs => window_seconds * 10);
 RETURN probe_count <= max_lookups;
END
$f$;
-- The synthetic envelope is verified inside the resolver boundary so
-- signed bytes never leave it. The manifest is the canonical JSON object
-- emitted by the synthetic provider; removing the literal signature
-- member reproduces the exact canonical unsigned form the HMAC covers.
-- Every failure returns false: malformed envelopes, wrong bindings and
-- bad signatures are indistinguishable.
CREATE FUNCTION clinic_app.prescription_verify_synthetic(
 signed_bytes pg_catalog.bytea,
 content_digest pg_catalog.text,
 signer_subject pg_catalog.text,
 operation_ref pg_catalog.text,
 not_before pg_catalog.timestamptz
)
RETURNS boolean LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 marker_at pg_catalog.int4;
 next_at pg_catalog.int4;
 manifest_text pg_catalog.text;
 manifest pg_catalog.jsonb;
 pair_text pg_catalog.text;
 unsigned_text pg_catalog.text;
 signed_at pg_catalog.timestamptz;
BEGIN
 IF signed_bytes IS NULL OR content_digest IS NULL
    OR signer_subject IS NULL OR operation_ref IS NULL
    OR not_before IS NULL THEN
   RETURN false;
 END IF;
 IF pg_catalog.octet_length(signed_bytes) < 17
    OR pg_catalog.substr(signed_bytes,
       pg_catalog.octet_length(signed_bytes) - 16, 17)
       <> pg_catalog.convert_to(E'\n%%END-SIGNATURE\n', 'UTF8') THEN
   RETURN false;
 END IF;
 -- The manifest follows the last marker, exactly like the provider's
 -- rfind: an earlier marker inside the content is not the envelope.
 marker_at := 0;
 LOOP
   next_at := position(
     pg_catalog.convert_to(E'\n%%SYNTHETIC-SIGNATURE\n', 'UTF8')
     IN pg_catalog.substr(signed_bytes, marker_at + 1));
   EXIT WHEN next_at = 0;
   marker_at := marker_at + next_at;
 END LOOP;
 IF marker_at = 0 THEN
   RETURN false;
 END IF;
 BEGIN
   manifest_text := pg_catalog.convert_from(
     pg_catalog.substr(signed_bytes, marker_at + 23,
       pg_catalog.octet_length(signed_bytes) - marker_at - 39),
     'UTF8');
   manifest := manifest_text::pg_catalog.jsonb;
 EXCEPTION WHEN OTHERS THEN
   RETURN false;
 END;
 IF pg_catalog.jsonb_typeof(manifest) <> 'object'
    OR (SELECT pg_catalog.count(*)
        FROM pg_catalog.jsonb_object_keys(manifest)) <> 6
    OR NOT manifest ?& ARRAY['v', 'operation_id', 'content_digest',
        'signer', 'signed_at', 'signature']
    OR pg_catalog.jsonb_typeof(manifest->'v') <> 'string'
    OR pg_catalog.jsonb_typeof(manifest->'operation_id') <> 'string'
    OR pg_catalog.jsonb_typeof(manifest->'content_digest') <> 'string'
    OR pg_catalog.jsonb_typeof(manifest->'signer') <> 'string'
    OR pg_catalog.jsonb_typeof(manifest->'signed_at') <> 'string'
    OR pg_catalog.jsonb_typeof(manifest->'signature') <> 'string'
    OR manifest->>'v' <> 'clinic-synthetic-signature-v1'
    OR manifest->>'operation_id' <> operation_ref
    OR manifest->>'signer' <> signer_subject
    OR manifest->>'content_digest' <> content_digest
    OR manifest->>'signature' !~ '^[0-9a-f]{64}$'
    OR pg_catalog.encode(
      clinic_app.digest(
        pg_catalog.substr(signed_bytes, 1, marker_at - 1), 'sha256'),
      'hex') <> content_digest THEN
   RETURN false;
 END IF;
 -- The signature member is always a 64-hex literal, so it needs no
 -- escaping; removing it yields the canonical unsigned manifest.
 pair_text := ',"signature":"' || (manifest->>'signature') || '"';
 unsigned_text := pg_catalog.replace(manifest_text, pair_text, '');
 IF unsigned_text = manifest_text
    OR pg_catalog.encode(
      clinic_app.hmac(
        pg_catalog.convert_to(unsigned_text, 'UTF8'),
        pg_catalog.convert_to(
          'synthetic-signing-key-not-a-credential', 'UTF8'),
        'sha256'),
      'hex') <> manifest->>'signature' THEN
   RETURN false;
 END IF;
 BEGIN
   IF manifest->>'signed_at'
      !~* '(z|[+-][0-9]{2}(:?[0-9]{2}(:[0-9]{2})?)?)$' THEN
     RETURN false;
   END IF;
   signed_at := (manifest->>'signed_at')::pg_catalog.timestamptz;
 EXCEPTION WHEN OTHERS THEN
   RETURN false;
 END;
 RETURN signed_at >= not_before
   AND signed_at <= pg_catalog.statement_timestamp();
END
$f$;
CREATE FUNCTION clinic_app.prescription_verify(
 requested_handle pg_catalog.text,
 allow_synthetic pg_catalog.bool
)
RETURNS TABLE(
 status pg_catalog.text,
 document_version pg_catalog.int4,
 issued_at pg_catalog.timestamptz,
 content_digest pg_catalog.text,
 signed_digest pg_catalog.text,
 issuer_label pg_catalog.text,
 clinic_label pg_catalog.text
)
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 d clinic_app.prescription_prescriptiondocument;
 o clinic_app.prescription_signatureoperation;
BEGIN
 -- Unknown, malformed and unissued handles are indistinguishable.
 IF requested_handle IS NULL
    OR pg_catalog.char_length(requested_handle) < 32
    OR pg_catalog.char_length(requested_handle) > 64
    OR requested_handle !~ '^[A-Za-z0-9_-]+$' THEN
   RETURN QUERY SELECT 'unavailable'::pg_catalog.text,
     NULL::pg_catalog.int4, NULL::pg_catalog.timestamptz,
     NULL::pg_catalog.text, NULL::pg_catalog.text,
     NULL::pg_catalog.text, NULL::pg_catalog.text;
   RETURN;
 END IF;
 SELECT * INTO d FROM clinic_app.prescription_prescriptiondocument
  WHERE qr_handle = requested_handle;
 IF d.id IS NULL THEN
   RETURN QUERY SELECT 'unavailable'::pg_catalog.text,
     NULL::pg_catalog.int4, NULL::pg_catalog.timestamptz,
     NULL::pg_catalog.text, NULL::pg_catalog.text,
     NULL::pg_catalog.text, NULL::pg_catalog.text;
   RETURN;
 END IF;
 -- Stored bytes that no longer match their recorded digests can never
 -- verify as anything but invalid; the status is still published.
 IF pg_catalog.encode(
      clinic_app.digest(d.pdf_bytes, 'sha256'), 'hex') <> d.pdf_digest THEN
   RETURN QUERY SELECT 'invalid'::pg_catalog.text, d.document_version,
     NULL::pg_catalog.timestamptz,
     NULL::pg_catalog.text, NULL::pg_catalog.text,
     NULL::pg_catalog.text, NULL::pg_catalog.text;
   RETURN;
 END IF;
 SELECT * INTO o FROM clinic_app.prescription_signatureoperation op
  WHERE op.document_id = d.id
    AND op.state IN ('issued', 'rehearsal_complete')
  ORDER BY op.completed_at DESC, op.id LIMIT 1;
 IF o.id IS NULL THEN
   RETURN QUERY SELECT 'unavailable'::pg_catalog.text,
     NULL::pg_catalog.int4, NULL::pg_catalog.timestamptz,
     NULL::pg_catalog.text, NULL::pg_catalog.text,
     NULL::pg_catalog.text, NULL::pg_catalog.text;
   RETURN;
 END IF;
 IF pg_catalog.encode(
      clinic_app.digest(o.signed_bytes, 'sha256'), 'hex')
    <> o.signed_digest THEN
   RETURN QUERY SELECT 'invalid'::pg_catalog.text, d.document_version,
     NULL::pg_catalog.timestamptz,
     NULL::pg_catalog.text, NULL::pg_catalog.text,
     NULL::pg_catalog.text, NULL::pg_catalog.text;
   RETURN;
 END IF;
 IF EXISTS (SELECT 1
    FROM clinic_app.prescription_prescriptiondocumentrevocation rv
    WHERE rv.document_id = d.id) THEN
   RETURN QUERY SELECT 'revoked'::pg_catalog.text, d.document_version,
     o.completed_at, d.pdf_digest::pg_catalog.text,
     o.signed_digest::pg_catalog.text,
     (d.frozen_input->>'issuer_label')::pg_catalog.text,
     (d.frozen_input->>'clinic_label')::pg_catalog.text;
   RETURN;
 END IF;
 IF EXISTS (SELECT 1
    FROM clinic_app.prescription_prescriptiondocument newer
    WHERE newer.draft_id = d.draft_id
      AND newer.document_version > d.document_version
      AND EXISTS (SELECT 1
        FROM clinic_app.prescription_signatureoperation newer_op
        WHERE newer_op.document_id = newer.id
          AND newer_op.state IN ('issued', 'rehearsal_complete'))) THEN
   RETURN QUERY SELECT 'superseded'::pg_catalog.text, d.document_version,
     o.completed_at, d.pdf_digest::pg_catalog.text,
     o.signed_digest::pg_catalog.text,
     (d.frozen_input->>'issuer_label')::pg_catalog.text,
     (d.frozen_input->>'clinic_label')::pg_catalog.text;
   RETURN;
 END IF;
 -- The signature is verified inside this privileged boundary; only the
 -- verdict is published. Signed bytes never leave the resolver, so the
 -- public handle can never grant document access. A disabled or unknown
 -- provider reports unavailable, never a false valid status.
 IF o.provider <> 'synthetic-signature-v1'
    OR allow_synthetic IS NOT TRUE THEN
   RETURN QUERY SELECT 'unavailable'::pg_catalog.text,
     NULL::pg_catalog.int4, NULL::pg_catalog.timestamptz,
     NULL::pg_catalog.text, NULL::pg_catalog.text,
     NULL::pg_catalog.text, NULL::pg_catalog.text;
   RETURN;
 END IF;
 IF NOT clinic_app.prescription_verify_synthetic(
   o.signed_bytes, d.pdf_digest, o.signer_subject,
   o.operation_id, o.created_at) THEN
   RETURN QUERY SELECT 'invalid'::pg_catalog.text, d.document_version,
     NULL::pg_catalog.timestamptz,
     NULL::pg_catalog.text, NULL::pg_catalog.text,
     NULL::pg_catalog.text, NULL::pg_catalog.text;
   RETURN;
 END IF;
 RETURN QUERY SELECT o.state::pg_catalog.text, d.document_version,
   o.completed_at, d.pdf_digest::pg_catalog.text,
   o.signed_digest::pg_catalog.text,
   (d.frozen_input->>'issuer_label')::pg_catalog.text,
   (d.frozen_input->>'clinic_label')::pg_catalog.text;
END
$f$;
CREATE FUNCTION clinic_app.prescription_patient_documents()
RETURNS TABLE(document_id pg_catalog.uuid,
              document_version pg_catalog.int4,
              state pg_catalog.text,
              issued_at pg_catalog.timestamptz,
              verification_url pg_catalog.text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT d.id, d.document_version, o.state::pg_catalog.text,
   o.completed_at, (d.frozen_input->>'verification_url')::pg_catalog.text
   FROM clinic_app.prescription_prescriptiondocumentrelease r
   JOIN clinic_app.prescription_prescriptiondocument d
     ON d.id = r.document_id
   JOIN clinic_app.prescription_signatureoperation o
     ON o.document_id = d.id
     AND o.state IN ('issued', 'rehearsal_complete')
   WHERE r.revoked_at IS NULL
   AND NOT EXISTS (SELECT 1
     FROM clinic_app.prescription_prescriptiondocumentrevocation rv
     WHERE rv.document_id = d.id)
   AND EXISTS (SELECT 1 FROM clinic_app.retention_records_session() s
     WHERE s.organization_id = r.organization_id
     AND s.clinic_id = r.clinic_id AND s.patient_id = r.patient_id)
   ORDER BY d.document_version, d.id
$f$;
CREATE FUNCTION clinic_app.prescription_patient_document_bytes(
 requested_document pg_catalog.uuid
)
RETURNS pg_catalog.bytea
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 -- NULL for every failure: unknown, unreleased, revoked, unsigned and
 -- digest-mismatched documents are indistinguishable to the patient.
 SELECT o.signed_bytes
   FROM clinic_app.prescription_prescriptiondocumentrelease r
   JOIN clinic_app.prescription_prescriptiondocument d
     ON d.id = r.document_id
   JOIN clinic_app.prescription_signatureoperation o
     ON o.document_id = d.id
     AND o.state IN ('issued', 'rehearsal_complete')
   WHERE r.document_id = requested_document
   AND r.revoked_at IS NULL
   AND NOT EXISTS (SELECT 1
     FROM clinic_app.prescription_prescriptiondocumentrevocation rv
     WHERE rv.document_id = d.id)
   AND EXISTS (SELECT 1 FROM clinic_app.retention_records_session() s
     WHERE s.organization_id = r.organization_id
     AND s.clinic_id = r.clinic_id AND s.patient_id = r.patient_id)
   AND pg_catalog.encode(
     clinic_app.digest(o.signed_bytes, 'sha256'), 'hex') = o.signed_digest
$f$;
REVOKE ALL ON FUNCTION clinic_app.prescription_verify_allowance(
  pg_catalog.bytea, pg_catalog.int4, pg_catalog.int4),
 clinic_app.prescription_verify_synthetic(
  pg_catalog.bytea, pg_catalog.text, pg_catalog.text,
  pg_catalog.text, pg_catalog.timestamptz),
 clinic_app.prescription_verify(pg_catalog.text, pg_catalog.bool),
 clinic_app.prescription_patient_documents(),
 clinic_app.prescription_patient_document_bytes(pg_catalog.uuid)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.prescription_verify_allowance(
  pg_catalog.bytea, pg_catalog.int4, pg_catalog.int4),
 clinic_app.prescription_verify(pg_catalog.text, pg_catalog.bool),
 clinic_app.prescription_patient_documents(),
 clinic_app.prescription_patient_document_bytes(pg_catalog.uuid)
 TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.prescription_patient_documents(),
 clinic_app.prescription_patient_document_bytes(pg_catalog.uuid)
 TO clinic_owner;
RESET ROLE;
""",
    """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.prescription_release_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE d clinic_app.prescription_prescriptiondocument;
BEGIN
 IF TG_OP = 'INSERT' THEN
   SELECT * INTO d FROM clinic_app.prescription_prescriptiondocument
    WHERE id = NEW.document_id;
   IF d.id IS NULL
      OR (d.organization_id,d.clinic_id,d.encounter_id,d.patient_id,
          d.issuer_id)
         IS DISTINCT FROM
         (NEW.organization_id,NEW.clinic_id,NEW.encounter_id,NEW.patient_id,
          NEW.issuer_id)
      OR NEW.revoked_at IS NOT NULL OR NEW.revoked_by_id IS NOT NULL
      OR NOT EXISTS (SELECT 1
        FROM clinic_app.prescription_signatureoperation o
        WHERE o.document_id = d.id
          AND o.state IN ('issued', 'rehearsal_complete')) THEN
     RAISE EXCEPTION 'invalid prescription release binding'
       USING ERRCODE='23514';
   END IF;
   RETURN NEW;
 END IF;
 IF (NEW.id,NEW.organization_id,NEW.document_id,NEW.encounter_id,
     NEW.clinic_id,NEW.patient_id,NEW.issuer_id,NEW.released_by_id,
     NEW.created_at)
    IS DISTINCT FROM
    (OLD.id,OLD.organization_id,OLD.document_id,OLD.encounter_id,
     OLD.clinic_id,OLD.patient_id,OLD.issuer_id,OLD.released_by_id,
     OLD.created_at) THEN
   RAISE EXCEPTION 'immutable prescription release binding'
     USING ERRCODE='23514';
 END IF;
 IF OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL
    AND NEW.revoked_by_id IS NOT NULL THEN
   RETURN NEW;
 END IF;
 RAISE EXCEPTION 'invalid prescription release transition'
   USING ERRCODE='23514';
END $f$;
CREATE FUNCTION clinic_app.prescription_revocation_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE d clinic_app.prescription_prescriptiondocument;
BEGIN
 SELECT * INTO d FROM clinic_app.prescription_prescriptiondocument
  WHERE id = NEW.document_id;
 IF d.id IS NULL
    OR (d.organization_id,d.clinic_id,d.encounter_id,d.patient_id,
        d.issuer_id)
       IS DISTINCT FROM
       (NEW.organization_id,NEW.clinic_id,NEW.encounter_id,NEW.patient_id,
        NEW.issuer_id)
    OR NEW.reason NOT IN ('clinical_error','issuance_error',
      'issuer_request','patient_request')
    OR NOT EXISTS (SELECT 1
      FROM clinic_app.prescription_signatureoperation o
      WHERE o.document_id = d.id
        AND o.state IN ('issued', 'rehearsal_complete')) THEN
   RAISE EXCEPTION 'invalid prescription revocation binding'
     USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.prescription_release_guard(),
 clinic_app.prescription_revocation_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.prescription_release_guard(),
 clinic_app.prescription_revocation_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
""",
    """
ALTER TABLE clinic_app.prescription_prescriptiondocumentrelease
 ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_prescriptiondocumentrelease
 FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_prescriptiondocumentrevocation
 ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_prescriptiondocumentrevocation
 FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_verificationprobe
 ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_verificationprobe
 FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant
 ON clinic_app.prescription_prescriptiondocumentrelease TO clinic_owner
 USING (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY setup_tenant
 ON clinic_app.prescription_prescriptiondocumentrevocation TO clinic_owner
 USING (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
REVOKE ALL ON clinic_app.prescription_prescriptiondocumentrelease,
 clinic_app.prescription_prescriptiondocumentrevocation,
 clinic_app.prescription_verificationprobe FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.prescription_prescriptiondocumentrelease,
 clinic_app.prescription_prescriptiondocumentrevocation TO clinic_app;
GRANT UPDATE (revoked_by_id, revoked_at)
 ON clinic_app.prescription_prescriptiondocumentrelease TO clinic_app;
CREATE POLICY release_read
 ON clinic_app.prescription_prescriptiondocumentrelease
 FOR SELECT TO clinic_app
 USING (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.ehr_care(encounter_id));
CREATE POLICY release_insert
 ON clinic_app.prescription_prescriptiondocumentrelease
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND released_by_id=
   NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE POLICY release_revoke
 ON clinic_app.prescription_prescriptiondocumentrelease
 FOR UPDATE TO clinic_app
 USING (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id))
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND revoked_by_id=
   NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE POLICY revocation_read
 ON clinic_app.prescription_prescriptiondocumentrevocation
 FOR SELECT TO clinic_app
 USING (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.ehr_care(encounter_id));
CREATE POLICY revocation_insert
 ON clinic_app.prescription_prescriptiondocumentrevocation
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND revoked_by_id=
   NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE TRIGGER prescription_release_binding BEFORE INSERT OR UPDATE
 ON clinic_app.prescription_prescriptiondocumentrelease FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_release_guard();
CREATE TRIGGER prescription_release_immutable BEFORE DELETE
 ON clinic_app.prescription_prescriptiondocumentrelease FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER prescription_revocation_binding BEFORE INSERT
 ON clinic_app.prescription_prescriptiondocumentrevocation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_revocation_guard();
CREATE TRIGGER prescription_revocation_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.prescription_prescriptiondocumentrevocation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.prescription_release_guard(),
 clinic_app.prescription_revocation_guard(),
 clinic_app.questionnaire_immutable() FROM clinic_owner;
RESET ROLE;
""",
    """
-- clinic_app can only ask for the fixed prescription.document.downloaded
-- event, and the function itself re-validates the live records session
-- and the release binding before chaining the row. The session id fills
-- actor_user_id so the actor-scope check holds without impersonating any
-- staff user.
CREATE FUNCTION clinic_app.prescription_document_viewed(
 requested_document pg_catalog.uuid,
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
 -- The session-scoped resolver is the only release authority here.
 IF NOT EXISTS (SELECT 1
   FROM clinic_app.prescription_patient_documents() p
   WHERE p.document_id = requested_document) THEN
   RAISE EXCEPTION 'document not released to this session'
     USING ERRCODE = '42501';
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
   session_row.organization_id, session_row.session_id,
   'prescription.document.downloaded',
   'clinic-os-web', NULL, 'prescription.document',
   requested_document::text, occurred_at_utc,
   pg_catalog.jsonb_build_object('clinic_id', session_row.clinic_id::text,
     'object_verb', 'downloaded'),
   previous_hash, current_hash)
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END
$f$;
ALTER FUNCTION clinic_app.prescription_document_viewed(
 uuid, timestamptz, bytea) OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.prescription_document_viewed(
 uuid, timestamptz, bytea) FROM PUBLIC, clinic_app, clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.prescription_document_viewed(
 uuid, timestamptz, bytea) TO clinic_app;
""",
]
SQL = "".join(_parts)

REVERSE_SQL = """
DROP TRIGGER prescription_release_binding
 ON clinic_app.prescription_prescriptiondocumentrelease;
DROP TRIGGER prescription_release_immutable
 ON clinic_app.prescription_prescriptiondocumentrelease;
DROP TRIGGER prescription_revocation_binding
 ON clinic_app.prescription_prescriptiondocumentrevocation;
DROP TRIGGER prescription_revocation_immutable
 ON clinic_app.prescription_prescriptiondocumentrevocation;
DROP POLICY release_read
 ON clinic_app.prescription_prescriptiondocumentrelease;
DROP POLICY release_insert
 ON clinic_app.prescription_prescriptiondocumentrelease;
DROP POLICY release_revoke
 ON clinic_app.prescription_prescriptiondocumentrelease;
DROP POLICY revocation_read
 ON clinic_app.prescription_prescriptiondocumentrevocation;
DROP POLICY revocation_insert
 ON clinic_app.prescription_prescriptiondocumentrevocation;
DROP POLICY setup_tenant
 ON clinic_app.prescription_prescriptiondocumentrelease;
DROP POLICY setup_tenant
 ON clinic_app.prescription_prescriptiondocumentrevocation;
DROP FUNCTION clinic_app.prescription_document_viewed(
 uuid, timestamptz, bytea);
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.prescription_patient_documents(),
 clinic_app.prescription_patient_document_bytes(pg_catalog.uuid)
 FROM clinic_owner;
DROP FUNCTION clinic_app.prescription_release_guard();
DROP FUNCTION clinic_app.prescription_revocation_guard();
DROP FUNCTION clinic_app.prescription_verify_allowance(
 pg_catalog.bytea, pg_catalog.int4, pg_catalog.int4);
DROP FUNCTION clinic_app.prescription_verify_synthetic(
 pg_catalog.bytea, pg_catalog.text, pg_catalog.text,
 pg_catalog.text, pg_catalog.timestamptz);
DROP FUNCTION clinic_app.prescription_verify(
 pg_catalog.text, pg_catalog.bool);
DROP FUNCTION clinic_app.prescription_patient_documents();
DROP FUNCTION clinic_app.prescription_patient_document_bytes(
 pg_catalog.uuid);
RESET ROLE;
"""
