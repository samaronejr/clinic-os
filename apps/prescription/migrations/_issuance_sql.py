"""Immutable synthetic issuance receipts; no real class gate is widened."""

SQL = """
REVOKE ALL ON clinic_app.prescription_prescriptionissuance,
 clinic_app.prescription_sncrnumberallocation FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.prescription_prescriptionissuance TO clinic_app;
GRANT SELECT ON clinic_app.prescription_sncrnumberallocation TO clinic_app;

SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.prescription_issuance_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE d clinic_app.prescription_prescriptiondocument;
        s clinic_app.prescription_signatureoperation;
BEGIN
 IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
   WHERE c.id=NEW.clinic_id AND c.organization_id=NEW.organization_id)
  OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
   WHERE r.clinic_id=NEW.clinic_id AND r.organization_id=NEW.organization_id
    AND r.user_id=NEW.issuer_id AND r.role='physician' AND r.revoked_at IS NULL)
 THEN RAISE EXCEPTION 'invalid issuance scope' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='prescription_sncrnumberallocation' THEN
  RETURN NEW;
 END IF;
 SELECT * INTO d FROM clinic_app.prescription_prescriptiondocument
  WHERE id=NEW.document_id;
 -- Share the existing draft guard fence without granting resolver UPDATE.
 PERFORM pg_advisory_xact_lock(hashtextextended(
  'clinical-document-draft:' || d.draft_id::text,0));
 SELECT * INTO s FROM clinic_app.prescription_signatureoperation
  WHERE id=NEW.signature_id;
 IF d.id IS NULL OR s.id IS NULL
  OR (d.organization_id,d.clinic_id,d.issuer_id)
     IS DISTINCT FROM (NEW.organization_id,NEW.clinic_id,NEW.issuer_id)
  OR (s.organization_id,s.clinic_id,s.issuer_id,s.document_id)
     IS DISTINCT FROM
     (NEW.organization_id,NEW.clinic_id,NEW.issuer_id,NEW.document_id)
  OR s.state<>'rehearsal_complete' OR NEW.state<>'rehearsal_complete'
  OR s.category IS DISTINCT FROM 'synthetic_non_controlled'
  OR s.content_digest<>d.pdf_digest
  OR NOT EXISTS (SELECT 1 FROM clinic_app.prescription_prescriptiondraft draft
   WHERE draft.id=d.draft_id AND draft.state='draft'
    AND draft.kind='prescription' AND draft.version=d.document_version
    AND draft.category='synthetic_non_controlled')
  OR EXISTS (SELECT 1 FROM clinic_app.prescription_prescriptiondocumentrevocation r
   WHERE r.document_id=d.id)
 THEN RAISE EXCEPTION 'invalid issuance binding' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.prescription_issuance_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.prescription_issuance_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
"""

for table in ("prescriptionissuance", "sncrnumberallocation"):
    SQL += """
ALTER TABLE clinic_app.prescription_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.prescription_{table} TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY issuer_read ON clinic_app.prescription_{table}
 FOR SELECT TO clinic_app
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
  AND EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
   WHERE r.clinic_id=prescription_{table}.clinic_id
    AND r.user_id=prescription_{table}.issuer_id
    AND r.role='physician' AND r.revoked_at IS NULL));
CREATE TRIGGER prescription_{table}_binding BEFORE INSERT
 ON clinic_app.prescription_{table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_issuance_guard();
CREATE TRIGGER prescription_{table}_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.prescription_{table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER prescription_{table}_no_truncate BEFORE TRUNCATE
 ON clinic_app.prescription_{table} FOR EACH STATEMENT
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
""".replace("{table}", table)

SQL += """
CREATE POLICY issuer_insert ON clinic_app.prescription_prescriptionissuance
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
  AND EXISTS (SELECT 1 FROM clinic_app.prescription_prescriptiondocument d
   WHERE d.id=document_id AND clinic_app.ehr_assigned(d.encounter_id)));
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.prescription_issuance_guard(),
 clinic_app.questionnaire_immutable() FROM clinic_owner;
RESET ROLE;
"""
