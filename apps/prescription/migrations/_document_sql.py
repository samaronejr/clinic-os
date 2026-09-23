"""FORCE RLS, least-privilege grants and insert-only artifact enforcement."""

_parts = [
    """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.prescription_document_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE d clinic_app.prescription_prescriptiondraft;
BEGIN
 SELECT * INTO d FROM clinic_app.prescription_prescriptiondraft
  WHERE id = NEW.draft_id;
 IF d.id IS NULL OR d.state <> 'draft'
    OR (d.organization_id,d.clinic_id,d.encounter_id,d.patient_id,d.issuer_id)
       IS DISTINCT FROM
       (NEW.organization_id,NEW.clinic_id,NEW.encounter_id,NEW.patient_id,
        NEW.issuer_id)
    OR NEW.document_version <> d.version THEN
   RAISE EXCEPTION 'invalid prescription document binding' USING ERRCODE='23514';
 END IF;
 IF NEW.state <> 'rendered'
    OR NEW.input_digest !~ '^[0-9a-f]{64}$'
    OR NEW.pdf_digest !~ '^[0-9a-f]{64}$'
    OR NEW.qr_handle !~ '^[A-Za-z0-9_-]{32,64}$'
    OR pg_catalog.substring(NEW.pdf_bytes, 1, 5) <> '\\x255044462d'::bytea
    OR pg_catalog.octet_length(NEW.pdf_bytes) > 2097152
    OR pg_catalog.jsonb_typeof(NEW.render_params) <> 'object'
    OR pg_catalog.jsonb_typeof(NEW.frozen_input) <> 'object' THEN
   RAISE EXCEPTION 'invalid prescription document content' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.prescription_document_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.prescription_document_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
""",
    """
ALTER TABLE clinic_app.prescription_prescriptiondocument
 ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_prescriptiondocument
 FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.prescription_prescriptiondocument
 TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
REVOKE ALL ON clinic_app.prescription_prescriptiondocument FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.prescription_prescriptiondocument TO clinic_app;
CREATE POLICY document_read ON clinic_app.prescription_prescriptiondocument
 FOR SELECT TO clinic_app
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.ehr_care(encounter_id));
CREATE POLICY document_insert ON clinic_app.prescription_prescriptiondocument
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE TRIGGER prescription_document_binding BEFORE INSERT
 ON clinic_app.prescription_prescriptiondocument FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_document_guard();
CREATE TRIGGER prescription_document_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.prescription_prescriptiondocument FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.prescription_document_guard(),
 clinic_app.questionnaire_immutable() FROM clinic_owner;
RESET ROLE;
""",
]
SQL = "".join(_parts)

REVERSE_SQL = """
DROP TRIGGER prescription_document_binding
 ON clinic_app.prescription_prescriptiondocument;
DROP TRIGGER prescription_document_immutable
 ON clinic_app.prescription_prescriptiondocument;
DROP POLICY document_read ON clinic_app.prescription_prescriptiondocument;
DROP POLICY document_insert ON clinic_app.prescription_prescriptiondocument;
DROP POLICY setup_tenant ON clinic_app.prescription_prescriptiondocument;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.prescription_document_guard();
RESET ROLE;
"""
