"""FORCE RLS, transition guards and resolver functions for signing."""

_parts = [
    """
GRANT SELECT ON clinic_app.prescription_signatureoperation,
 clinic_app.prescription_signaturecallback,
 clinic_app.prescription_prescriptiondocument TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.prescription_signature_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE d clinic_app.prescription_prescriptiondocument;
BEGIN
 IF TG_OP = 'INSERT' THEN
   SELECT * INTO d FROM clinic_app.prescription_prescriptiondocument
    WHERE id = NEW.document_id;
   IF d.id IS NULL
      OR (d.organization_id,d.clinic_id,d.encounter_id,d.patient_id,d.issuer_id)
         IS DISTINCT FROM
         (NEW.organization_id,NEW.clinic_id,NEW.encounter_id,NEW.patient_id,
          NEW.issuer_id)
      OR NEW.state <> 'draft'
      OR NEW.operation_id <> ''
      OR NEW.content_digest <> d.pdf_digest
      OR btrim(NEW.signer_subject) = ''
      OR NEW.evidence_id IS NOT NULL
      OR NEW.evidence_snapshot IS NOT NULL
      OR NEW.authorized_until IS NOT NULL
      OR NEW.signed_bytes IS NOT NULL
      OR NEW.signed_digest IS NOT NULL
      OR NEW.completed_at IS NOT NULL
      OR NEW.failure_reason <> '' THEN
     RAISE EXCEPTION 'invalid signature operation binding' USING ERRCODE='23514';
   END IF;
   RETURN NEW;
 END IF;
 IF (NEW.id,NEW.organization_id,NEW.document_id,NEW.encounter_id,NEW.clinic_id,
     NEW.patient_id,NEW.issuer_id,NEW.provider,NEW.content_digest,
     NEW.signer_subject,NEW.created_at)
    IS DISTINCT FROM
    (OLD.id,OLD.organization_id,OLD.document_id,OLD.encounter_id,OLD.clinic_id,
     OLD.patient_id,OLD.issuer_id,OLD.provider,OLD.content_digest,
     OLD.signer_subject,OLD.created_at) THEN
   RAISE EXCEPTION 'immutable signature operation binding' USING ERRCODE='23514';
 END IF;
 IF OLD.state = 'draft' AND NEW.state = 'prepared'
    AND NEW.evidence_id IS NOT NULL
    AND pg_catalog.jsonb_typeof(NEW.evidence_snapshot) = 'object'
    AND NEW.authorized_until IS NOT NULL
    AND NEW.operation_id = '' AND NEW.signed_bytes IS NULL
    AND NEW.signed_digest IS NULL AND NEW.completed_at IS NULL
    AND NEW.failure_reason = '' THEN
   RETURN NEW;
 END IF;
 IF OLD.state = 'prepared' AND NEW.state = 'signing'
    AND NEW.operation_id <> '' AND NEW.operation_id <> OLD.operation_id
    AND NEW.evidence_id IS NOT NULL
    AND NEW.evidence_id IS NOT DISTINCT FROM OLD.evidence_id
    AND NEW.evidence_snapshot IS NOT DISTINCT FROM OLD.evidence_snapshot
    AND NEW.authorized_until IS NOT DISTINCT FROM OLD.authorized_until
    AND NEW.signed_bytes IS NULL AND NEW.signed_digest IS NULL
    AND NEW.completed_at IS NULL AND NEW.failure_reason = '' THEN
   RETURN NEW;
 END IF;
 IF OLD.state = 'signing' AND NEW.state IN ('issued','rehearsal_complete')
    AND NEW.operation_id IS NOT DISTINCT FROM OLD.operation_id
    AND NEW.evidence_id IS NOT DISTINCT FROM OLD.evidence_id
    AND NEW.evidence_snapshot IS NOT DISTINCT FROM OLD.evidence_snapshot
    AND NEW.authorized_until IS NOT DISTINCT FROM OLD.authorized_until
    AND NEW.signed_bytes IS NOT NULL
    AND NEW.signed_digest ~ '^[0-9a-f]{64}$'
    AND NEW.completed_at IS NOT NULL
    AND NEW.failure_reason = '' THEN
   RETURN NEW;
 END IF;
 IF NEW.state = 'failed' AND OLD.state IN ('draft','prepared','signing')
    AND NEW.operation_id IS NOT DISTINCT FROM OLD.operation_id
    AND NEW.evidence_id IS NOT DISTINCT FROM OLD.evidence_id
    AND NEW.evidence_snapshot IS NOT DISTINCT FROM OLD.evidence_snapshot
    AND NEW.authorized_until IS NOT DISTINCT FROM OLD.authorized_until
    AND NEW.signed_bytes IS NULL AND NEW.signed_digest IS NULL
    AND NEW.completed_at IS NOT NULL
    AND NEW.failure_reason <> '' THEN
   RETURN NEW;
 END IF;
 RAISE EXCEPTION 'invalid signature state transition' USING ERRCODE='23514';
END $f$;
CREATE FUNCTION clinic_app.prescription_signature_callback_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE o clinic_app.prescription_signatureoperation;
BEGIN
 SELECT * INTO o FROM clinic_app.prescription_signatureoperation
  WHERE id = NEW.operation_id;
 IF o.id IS NULL OR o.organization_id <> NEW.organization_id
    OR btrim(NEW.event_id) = ''
    OR pg_catalog.jsonb_typeof(NEW.payload) <> 'object'
    OR NEW.verified_at IS NULL THEN
   RAISE EXCEPTION 'invalid signature callback binding' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.prescription_signature_scope(
 requested_operation pg_catalog.uuid
)
RETURNS TABLE(
 organization_id pg_catalog.uuid,
 clinic_id pg_catalog.uuid,
 actor_id pg_catalog.uuid,
 provider pg_catalog.text
)
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT operation.organization_id, operation.clinic_id,
        operation.issuer_id, operation.provider
 FROM clinic_app.prescription_signatureoperation AS operation
 WHERE operation.id = requested_operation
$f$;
CREATE FUNCTION clinic_app.prescription_signature_callback_scope(
 requested_provider pg_catalog.text,
 requested_reference pg_catalog.text
)
RETURNS TABLE(
 operation_id pg_catalog.uuid,
 organization_id pg_catalog.uuid,
 clinic_id pg_catalog.uuid,
 actor_id pg_catalog.uuid,
 provider pg_catalog.text
)
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT operation.id, operation.organization_id, operation.clinic_id,
        operation.issuer_id, operation.provider
 FROM clinic_app.prescription_signatureoperation AS operation
 WHERE operation.provider = requested_provider
   AND operation.operation_id = requested_reference
$f$;
REVOKE ALL ON FUNCTION clinic_app.prescription_signature_guard(),
 clinic_app.prescription_signature_callback_guard() FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION
 clinic_app.prescription_signature_scope(pg_catalog.uuid) FROM PUBLIC;
REVOKE ALL PRIVILEGES ON FUNCTION
 clinic_app.prescription_signature_callback_scope(
   pg_catalog.text, pg_catalog.text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.prescription_signature_guard(),
 clinic_app.prescription_signature_callback_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
GRANT EXECUTE ON FUNCTION
 clinic_app.prescription_signature_scope(pg_catalog.uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION
 clinic_app.prescription_signature_callback_scope(
   pg_catalog.text, pg_catalog.text) TO clinic_app;
RESET ROLE;
""",
    """
ALTER TABLE clinic_app.prescription_signatureoperation
 ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_signatureoperation
 FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_signaturecallback
 ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_signaturecallback
 FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.prescription_signatureoperation
 TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY setup_tenant ON clinic_app.prescription_signaturecallback
 TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
REVOKE ALL ON clinic_app.prescription_signatureoperation,
 clinic_app.prescription_signaturecallback FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.prescription_signatureoperation,
 clinic_app.prescription_signaturecallback TO clinic_app;
GRANT UPDATE (state, operation_id, evidence_id, evidence_snapshot,
 authorized_until, signed_bytes, signed_digest, failure_reason,
 completed_at)
 ON clinic_app.prescription_signatureoperation TO clinic_app;
CREATE POLICY signature_read ON clinic_app.prescription_signatureoperation
 FOR SELECT TO clinic_app
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE POLICY signature_insert ON clinic_app.prescription_signatureoperation
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE POLICY signature_update ON clinic_app.prescription_signatureoperation
 FOR UPDATE TO clinic_app
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id))
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE POLICY signature_callback_read ON clinic_app.prescription_signaturecallback
 FOR SELECT TO clinic_app
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.prescription_signatureoperation o
   WHERE o.id = prescription_signaturecallback.operation_id));
CREATE POLICY signature_callback_insert ON clinic_app.prescription_signaturecallback
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.prescription_signatureoperation o
   WHERE o.id = prescription_signaturecallback.operation_id
   AND o.issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
   AND clinic_app.ehr_assigned(o.encounter_id)));
CREATE TRIGGER prescription_signature_guard BEFORE INSERT OR UPDATE
 ON clinic_app.prescription_signatureoperation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_signature_guard();
CREATE TRIGGER prescription_signature_immutable BEFORE DELETE
 ON clinic_app.prescription_signatureoperation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER prescription_signature_callback_binding BEFORE INSERT
 ON clinic_app.prescription_signaturecallback FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_signature_callback_guard();
CREATE TRIGGER prescription_signature_callback_immutable
 BEFORE UPDATE OR DELETE ON clinic_app.prescription_signaturecallback
 FOR EACH ROW EXECUTE FUNCTION clinic_app.questionnaire_immutable();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.prescription_signature_guard(),
 clinic_app.prescription_signature_callback_guard(),
 clinic_app.questionnaire_immutable() FROM clinic_owner;
RESET ROLE;
""",
]
SQL = "".join(_parts)

REVERSE_SQL = """
DROP TRIGGER prescription_signature_guard
 ON clinic_app.prescription_signatureoperation;
DROP TRIGGER prescription_signature_immutable
 ON clinic_app.prescription_signatureoperation;
DROP TRIGGER prescription_signature_callback_binding
 ON clinic_app.prescription_signaturecallback;
DROP TRIGGER prescription_signature_callback_immutable
 ON clinic_app.prescription_signaturecallback;
DROP POLICY signature_read ON clinic_app.prescription_signatureoperation;
DROP POLICY signature_insert ON clinic_app.prescription_signatureoperation;
DROP POLICY signature_update ON clinic_app.prescription_signatureoperation;
DROP POLICY signature_callback_read ON clinic_app.prescription_signaturecallback;
DROP POLICY signature_callback_insert ON clinic_app.prescription_signaturecallback;
DROP POLICY setup_tenant ON clinic_app.prescription_signatureoperation;
DROP POLICY setup_tenant ON clinic_app.prescription_signaturecallback;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.prescription_signature_guard();
DROP FUNCTION clinic_app.prescription_signature_callback_guard();
DROP FUNCTION clinic_app.prescription_signature_scope(pg_catalog.uuid);
DROP FUNCTION clinic_app.prescription_signature_callback_scope(
 pg_catalog.text, pg_catalog.text);
RESET ROLE;
"""
