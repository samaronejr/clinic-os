"""Add a quarantined, immutable candidate state without widening any grants."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.prescription_signature_guard()
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
      OR NEW.state <> 'draft' OR NEW.operation_id <> ''
      OR NEW.content_digest <> d.pdf_digest OR btrim(NEW.signer_subject) = ''
      OR NEW.evidence_id IS NOT NULL OR NEW.evidence_snapshot IS NOT NULL
      OR NEW.authorized_until IS NOT NULL OR NEW.signed_bytes IS NOT NULL
      OR NEW.signed_digest IS NOT NULL OR NEW.completed_at IS NOT NULL
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
 -- Every transition after preparation preserves the complete approval.
 IF NEW.evidence_id IS DISTINCT FROM OLD.evidence_id
    OR NEW.evidence_snapshot IS DISTINCT FROM OLD.evidence_snapshot
    OR NEW.authorized_until IS DISTINCT FROM OLD.authorized_until THEN
   RAISE EXCEPTION 'immutable signature approval' USING ERRCODE='23514';
 END IF;
 IF OLD.state = 'prepared' AND NEW.state = 'signing'
    AND NEW.operation_id <> '' AND NEW.operation_id <> OLD.operation_id
    AND NEW.evidence_id IS NOT NULL
    AND NEW.signed_bytes IS NULL AND NEW.signed_digest IS NULL
    AND NEW.completed_at IS NULL AND NEW.failure_reason = '' THEN
   RETURN NEW;
 END IF;
 IF NEW.operation_id IS DISTINCT FROM OLD.operation_id THEN
   RAISE EXCEPTION 'immutable signature reference' USING ERRCODE='23514';
 END IF;
 IF OLD.state = 'signing' AND NEW.state = 'signed_unverified'
    AND OLD.provider = 'recorded-signature-v1'
    AND NEW.evidence_snapshot->>'approval_digest' ~ '^[0-9a-f]{64}$'
    AND NEW.signed_bytes IS NOT NULL
    AND NEW.signed_digest ~ '^[0-9a-f]{64}$'
    AND NEW.completed_at IS NULL AND NEW.failure_reason = '' THEN
   RETURN NEW;
 END IF;
 -- Preserve the legacy synthetic path, never extend it to another provider.
 IF OLD.state = 'signing' AND NEW.state = 'rehearsal_complete'
    AND OLD.provider = 'synthetic-signature-v1'
    AND NEW.signed_bytes IS NOT NULL
    AND NEW.signed_digest ~ '^[0-9a-f]{64}$'
    AND NEW.completed_at IS NOT NULL AND NEW.failure_reason = '' THEN
   RETURN NEW;
 END IF;
 IF OLD.state = 'signed_unverified'
    AND (NEW.signed_bytes IS DISTINCT FROM OLD.signed_bytes
         OR NEW.signed_digest IS DISTINCT FROM OLD.signed_digest) THEN
   RAISE EXCEPTION 'immutable signature candidate' USING ERRCODE='23514';
 END IF;
 IF OLD.state = 'signed_unverified' AND NEW.state = 'rehearsal_complete'
    AND OLD.provider = 'recorded-signature-v1'
    AND NEW.completed_at IS NOT NULL AND NEW.failure_reason = '' THEN
   RETURN NEW;
 END IF;
 IF NEW.state = 'failed' AND OLD.state IN (
     'draft','prepared','signing','signed_unverified')
    AND (OLD.state = 'signed_unverified'
         OR (NEW.signed_bytes IS NULL AND NEW.signed_digest IS NULL))
    AND NEW.completed_at IS NOT NULL AND NEW.failure_reason <> '' THEN
   RETURN NEW;
 END IF;
 RAISE EXCEPTION 'invalid signature state transition' USING ERRCODE='23514';
END $f$;
RESET ROLE;
"""
