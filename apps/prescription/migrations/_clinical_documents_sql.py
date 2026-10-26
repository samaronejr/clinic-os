"""Kind immutability, encrypted snapshots and current-version trust fences."""

SQL = """
REVOKE ALL ON clinic_app.prescription_documentdraftcontent FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.prescription_documentdraftcontent TO clinic_app;
ALTER TABLE clinic_app.prescription_documentdraftcontent ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_documentdraftcontent FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.prescription_documentdraftcontent
 TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY content_read ON clinic_app.prescription_documentdraftcontent
 FOR SELECT TO clinic_app
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.prescription_prescriptiondraft d
   WHERE d.id=draft_id AND d.state='draft'));
CREATE POLICY content_insert ON clinic_app.prescription_documentdraftcontent
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.prescription_prescriptiondraft d
   WHERE d.id=draft_id AND d.state='draft' AND d.kind<>'prescription'
     AND d.version=prescription_documentdraftcontent.version));

SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.prescription_draft_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE e clinic_app.ehr_encounter;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinical-document-draft:' || NEW.id::text,0));
 SELECT * INTO e FROM clinic_app.ehr_encounter WHERE id = NEW.encounter_id;
 IF e.id IS NULL OR (e.organization_id,e.clinic_id,e.patient_id,e.physician_id)
    IS DISTINCT FROM (NEW.organization_id,NEW.clinic_id,NEW.patient_id,NEW.issuer_id)
 THEN RAISE EXCEPTION 'invalid prescription binding' USING ERRCODE='23514'; END IF;
 IF TG_OP = 'INSERT' THEN
   IF e.state <> 'open' OR NEW.version <> 1 OR NEW.state <> 'draft' THEN
     RAISE EXCEPTION 'invalid initial prescription' USING ERRCODE='23514';
   END IF;
 ELSE
   IF (NEW.id,NEW.organization_id,NEW.encounter_id,NEW.clinic_id,NEW.patient_id,
       NEW.issuer_id,NEW.category,NEW.contract_version,NEW.created_at,NEW.kind)
      IS DISTINCT FROM
      (OLD.id,OLD.organization_id,OLD.encounter_id,OLD.clinic_id,OLD.patient_id,
       OLD.issuer_id,OLD.category,OLD.contract_version,OLD.created_at,OLD.kind)
      OR OLD.state <> 'draft' OR NEW.version <> OLD.version + 1
      OR e.state <> 'open' THEN
     RAISE EXCEPTION 'immutable prescription or stale version' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.prescription_content_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM clinic_app.prescription_prescriptiondraft d
   WHERE d.id=NEW.draft_id AND d.organization_id=NEW.organization_id
     AND d.kind<>'prescription' AND d.version=NEW.version AND d.state='draft')
   OR NEW.content IS NULL OR octet_length(NEW.content)>131072 THEN
   RAISE EXCEPTION 'invalid document content binding' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.prescription_current_document_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE d clinic_app.prescription_prescriptiondraft;
 artifact clinic_app.prescription_prescriptiondocument;
BEGIN
 IF NEW.state='failed' THEN RETURN NEW; END IF;
 SELECT * INTO artifact FROM clinic_app.prescription_prescriptiondocument
 WHERE id=NEW.document_id;
 PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinical-document-draft:' || artifact.draft_id::text,0));
 SELECT * INTO d FROM clinic_app.prescription_prescriptiondraft
 WHERE id=artifact.draft_id;
 IF d.id IS NULL OR d.state<>'draft'
   OR d.version<>artifact.document_version THEN
   RAISE EXCEPTION 'stale_document_approval' USING ERRCODE='23514';
 END IF;
 IF d.kind<>'prescription' AND NOT EXISTS (
   SELECT 1 FROM clinic_app.intake_patientclinicenrollment enrollment
   WHERE enrollment.patient_id=d.patient_id AND enrollment.clinic_id=d.clinic_id
     AND clinic_app.has_permission('prescription.prepare',d.clinic_id,enrollment.id)
     AND clinic_app.has_permission('prescription.sign',d.clinic_id,enrollment.id)
 ) THEN
   RAISE EXCEPTION 'document professional scope denied' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.prescription_content_guard(),
 clinic_app.prescription_current_document_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.prescription_content_guard(),
 clinic_app.prescription_current_document_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;

CREATE TRIGGER prescription_content_binding BEFORE INSERT
 ON clinic_app.prescription_documentdraftcontent FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_content_guard();
CREATE TRIGGER prescription_content_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.prescription_documentdraftcontent FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER prescription_current_signature BEFORE INSERT OR UPDATE
 ON clinic_app.prescription_signatureoperation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_current_document_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.prescription_content_guard(),
 clinic_app.prescription_current_document_guard(),
 clinic_app.questionnaire_immutable() FROM clinic_owner;
RESET ROLE;

-- Legacy prescription entry retains its original authority contract. Every new
-- kind additionally requires current registration and patient-scoped permission.
ALTER POLICY draft_read ON clinic_app.prescription_prescriptiondraft
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id)
 AND (kind='prescription' OR EXISTS (
   SELECT 1 FROM clinic_app.intake_patientclinicenrollment enrollment
   WHERE enrollment.patient_id=prescription_prescriptiondraft.patient_id
     AND enrollment.clinic_id=prescription_prescriptiondraft.clinic_id
     AND clinic_app.has_permission('prescription.prepare',enrollment.clinic_id,
       enrollment.id))));
ALTER POLICY draft_insert ON clinic_app.prescription_prescriptiondraft
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id)
 AND (kind='prescription' OR EXISTS (
   SELECT 1 FROM clinic_app.intake_patientclinicenrollment enrollment
   WHERE enrollment.patient_id=prescription_prescriptiondraft.patient_id
     AND enrollment.clinic_id=prescription_prescriptiondraft.clinic_id
     AND clinic_app.has_permission('prescription.prepare',enrollment.clinic_id,
       enrollment.id))));
"""
