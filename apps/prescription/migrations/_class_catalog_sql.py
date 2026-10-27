"""Additive taxonomy and private favorite guards; no electronic gate is opened."""

SQL = """
SET CONSTRAINTS ALL IMMEDIATE;
REVOKE ALL ON clinic_app.prescription_prescriptionclasspolicy,
 clinic_app.prescription_medicationcatalogimport,
 clinic_app.prescription_medicationcatalogentry FROM PUBLIC, clinic_app;
GRANT SELECT ON clinic_app.prescription_prescriptionclasspolicy,
 clinic_app.prescription_medicationcatalogimport,
 clinic_app.prescription_medicationcatalogentry TO clinic_app;
GRANT UPDATE(category) ON clinic_app.prescription_signatureoperation TO clinic_resolver;

-- Historical rows were bound to the only explicit class admitted by 0011.
-- Backfill the new metadata column, never rewrite an artifact or its bytes.
ALTER TABLE clinic_app.prescription_signatureoperation
 DISABLE TRIGGER prescription_signature_guard;
ALTER TABLE clinic_app.prescription_signatureoperation
 DISABLE TRIGGER prescription_current_signature;
SET LOCAL ROLE clinic_resolver;
UPDATE clinic_app.prescription_signatureoperation o SET category=d.category
 FROM clinic_app.prescription_prescriptiondocument p
 JOIN clinic_app.prescription_prescriptiondraft d ON d.id=p.draft_id
 WHERE o.document_id=p.id;
RESET ROLE;
ALTER TABLE clinic_app.prescription_signatureoperation
 ENABLE TRIGGER prescription_signature_guard;
ALTER TABLE clinic_app.prescription_signatureoperation
 ENABLE TRIGGER prescription_current_signature;
REVOKE UPDATE(category) ON clinic_app.prescription_signatureoperation
 FROM clinic_resolver;
GRANT UPDATE(category,contract_version)
 ON clinic_app.prescription_prescriptiondraft TO clinic_app;

SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.prescription_draft_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE e clinic_app.ehr_encounter;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinical-document-draft:' || NEW.id::text,0));
 SELECT * INTO e FROM clinic_app.ehr_encounter WHERE id=NEW.encounter_id;
 IF e.id IS NULL OR (e.organization_id,e.clinic_id,e.patient_id,e.physician_id)
 IS DISTINCT FROM (NEW.organization_id,NEW.clinic_id,NEW.patient_id,NEW.issuer_id)
 THEN RAISE EXCEPTION 'invalid prescription binding' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' THEN
  IF e.state<>'open' OR NEW.version<>1 OR NEW.state<>'draft' THEN
   RAISE EXCEPTION 'invalid initial prescription' USING ERRCODE='23514';
  END IF;
  IF NEW.renewal_source_id IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM clinic_app.prescription_prescriptiondocument s
   JOIN clinic_app.prescription_prescriptiondraft source ON source.id=s.draft_id
   WHERE s.id=NEW.renewal_source_id AND s.organization_id=NEW.organization_id
    AND source.kind='prescription' AND NEW.kind='prescription'
    AND s.clinic_id=NEW.clinic_id AND s.patient_id=NEW.patient_id
    AND clinic_app.ehr_care(s.encounter_id)
    AND EXISTS (SELECT 1 FROM clinic_app.prescription_signatureoperation o
     WHERE o.document_id=s.id AND o.state IN ('issued','rehearsal_complete'))
    AND NOT EXISTS (
     SELECT 1 FROM clinic_app.prescription_prescriptiondocumentrevocation r
     WHERE r.document_id=s.id)
  ) THEN
   RAISE EXCEPTION 'invalid renewal binding' USING ERRCODE='23514';
  END IF;
  IF NEW.renewal_source_id IS NOT NULL AND NEW.category IS NOT NULL THEN
   RAISE EXCEPTION 'renewal class must start unset' USING ERRCODE='23514';
  END IF;
 ELSE
  IF (NEW.id,NEW.organization_id,NEW.encounter_id,NEW.clinic_id,NEW.patient_id,
      NEW.issuer_id,NEW.renewal_source_id,NEW.created_at,NEW.kind)
   IS DISTINCT FROM
     (OLD.id,OLD.organization_id,OLD.encounter_id,OLD.clinic_id,OLD.patient_id,
      OLD.issuer_id,OLD.renewal_source_id,OLD.created_at,OLD.kind)
   OR OLD.state<>'draft' OR NEW.version<>OLD.version+1 OR e.state<>'open' THEN
   RAISE EXCEPTION 'immutable prescription or stale version' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $f$;

CREATE OR REPLACE FUNCTION clinic_app.prescription_item_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE d clinic_app.prescription_prescriptiondraft;
BEGIN
 SELECT * INTO d FROM clinic_app.prescription_prescriptiondraft WHERE id=NEW.draft_id;
 IF d.id IS NULL OR d.organization_id<>NEW.organization_id
  OR d.version<>NEW.version OR d.state<>'draft'
  OR NEW.medication_description IS NULL OR NEW.strength_form IS NULL
  OR NEW.dose IS NULL OR NEW.route IS NULL OR NEW.frequency IS NULL
  OR NEW.duration IS NULL OR NEW.quantity IS NULL THEN
  RAISE EXCEPTION 'invalid prescription item binding' USING ERRCODE='23514';
 END IF;
 IF NEW.medication_statement_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM clinic_app.ehr_medicationstatement m
  WHERE m.id=NEW.medication_statement_id AND m.organization_id=d.organization_id
   AND m.clinic_id=d.clinic_id AND m.patient_id=d.patient_id
 ) THEN
  RAISE EXCEPTION 'invalid medication statement binding' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.prescription_signature_class_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE selected_class text; current_version integer; rendered_version integer;
BEGIN
 IF TG_OP='INSERT' THEN
  SELECT d.category,d.version,p.document_version
   INTO selected_class,current_version,rendered_version
   FROM clinic_app.prescription_prescriptiondocument p
   JOIN clinic_app.prescription_prescriptiondraft d ON d.id=p.draft_id
   WHERE p.id=NEW.document_id;
  IF current_version IS NULL OR current_version<>rendered_version
   OR (NEW.category IS NOT NULL AND NEW.category IS DISTINCT FROM selected_class) THEN
   RAISE EXCEPTION 'invalid signature class binding' USING ERRCODE='23514';
  END IF;
  -- This copies an explicit selection, never infers one from medication text.
  NEW.category:=selected_class;
 ELSIF NEW.category IS DISTINCT FROM OLD.category THEN
  RAISE EXCEPTION 'immutable signature class' USING ERRCODE='23514';
 END IF;
 IF NEW.state='issued' AND NEW.category IS NOT NULL THEN
  RAISE EXCEPTION 'class_electronic_unavailable' USING ERRCODE='23514';
 END IF;
 IF NEW.state='rehearsal_complete'
  AND NEW.category IS DISTINCT FROM 'synthetic_non_controlled' THEN
  RAISE EXCEPTION 'class_electronic_unavailable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.prescription_favorite_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
  WHERE c.id=NEW.clinic_id AND c.organization_id=NEW.organization_id)
  OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
   WHERE r.clinic_id=NEW.clinic_id AND r.organization_id=NEW.organization_id
    AND r.user_id=NEW.issuer_id AND r.role='physician' AND r.revoked_at IS NULL)
 THEN RAISE EXCEPTION 'invalid favorite binding' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND
  (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.issuer_id,NEW.entry_id,NEW.created_at)
  IS DISTINCT FROM
  (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.issuer_id,OLD.entry_id,OLD.created_at)
 THEN RAISE EXCEPTION 'immutable favorite binding' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.prescription_signature_class_guard(),
 clinic_app.prescription_favorite_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.prescription_signature_class_guard(),
 clinic_app.prescription_favorite_guard(),clinic_app.questionnaire_immutable()
 TO clinic_owner;
RESET ROLE;

ALTER TABLE clinic_app.prescription_medicationfavorite ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_medicationfavorite FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.prescription_medicationfavorite FROM PUBLIC,clinic_app;
GRANT SELECT,INSERT ON clinic_app.prescription_medicationfavorite TO clinic_app;
GRANT UPDATE(active) ON clinic_app.prescription_medicationfavorite TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.prescription_medicationfavorite
 TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (
  organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY favorite_read ON clinic_app.prescription_medicationfavorite
 FOR SELECT TO clinic_app USING (
 organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,ARRAY['physician']::text[]));
CREATE POLICY favorite_insert ON clinic_app.prescription_medicationfavorite
 FOR INSERT TO clinic_app WITH CHECK (
 organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,ARRAY['physician']::text[]));
CREATE POLICY favorite_update ON clinic_app.prescription_medicationfavorite
 FOR UPDATE TO clinic_app USING (
 organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,ARRAY['physician']::text[]))
 WITH CHECK (
 organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,ARRAY['physician']::text[]));
CREATE TRIGGER prescription_favorite_binding BEFORE INSERT OR UPDATE
 ON clinic_app.prescription_medicationfavorite FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_favorite_guard();
CREATE TRIGGER prescription_favorite_immutable BEFORE DELETE
 ON clinic_app.prescription_medicationfavorite FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER prescription_signature_class_guard BEFORE INSERT OR UPDATE
 ON clinic_app.prescription_signatureoperation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_signature_class_guard();
CREATE TRIGGER prescription_class_policy_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.prescription_prescriptionclasspolicy FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER prescription_catalog_import_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.prescription_medicationcatalogimport FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER prescription_catalog_entry_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.prescription_medicationcatalogentry FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.prescription_signature_class_guard(),
 clinic_app.prescription_favorite_guard(),clinic_app.questionnaire_immutable()
 FROM clinic_owner;
RESET ROLE;
"""
