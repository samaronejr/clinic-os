"""Bundle binding and retained-history guards, independent of template schemas."""

SQL = """
GRANT SELECT ON clinic_app.ehr_specialtypack, clinic_app.ehr_specialtyentry
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ehr_pack_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE p clinic_app.ehr_specialtypack; e clinic_app.ehr_encounter;
 expected_calculators jsonb;
BEGIN
 IF TG_OP='DELETE' THEN
   RAISE EXCEPTION 'retained specialty history' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='ehr_specialtypack' THEN
   IF TG_OP='UPDATE' THEN
     PERFORM pg_advisory_xact_lock(hashtextextended(
       'ehr-pack-use:'||OLD.id::text,0));
     IF NEW.position IS DISTINCT FROM OLD.position THEN
       IF (to_jsonb(NEW)-'position') IS DISTINCT FROM (to_jsonb(OLD)-'position')
         OR OLD.uninstalled_at IS NOT NULL THEN
         RAISE EXCEPTION 'invalid pack ordering' USING ERRCODE='23514';
       END IF;
       RETURN NEW;
     END IF;
     IF (to_jsonb(NEW)-'uninstalled_at') IS DISTINCT FROM
        (to_jsonb(OLD)-'uninstalled_at') OR OLD.uninstalled_at IS NOT NULL
       OR NEW.uninstalled_at IS NULL THEN
       RAISE EXCEPTION 'invalid pack transition' USING ERRCODE='23514';
     END IF;
     RETURN NEW;
   END IF;
   expected_calculators := CASE NEW.key
     WHEN 'general' THEN '[{"key":"bmi","version":1},{"key":"egfr","version":1}]'
     WHEN 'pediatrics' THEN
       '[{"key":"corrected_age","version":1},{"key":"growth_percentile","version":1}]'
     WHEN 'psychiatry' THEN '[{"key":"phq9","version":1},{"key":"gad7","version":1}]'
     WHEN 'procedures' THEN '[]' END;
   IF NEW.version<>1 OR NEW.uninstalled_at IS NOT NULL
     OR NEW.calculators IS DISTINCT FROM expected_calculators
     OR NEW.license_ref<>'docs/clinical/specialty-packs.md'
     OR NEW.clinical_owner_id IS DISTINCT FROM
       NULLIF(current_setting('app.current_user_id',true),'')::uuid
     OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
       WHERE c.id=NEW.clinic_id AND c.organization_id=NEW.organization_id)
     OR NOT EXISTS (SELECT 1 FROM clinic_app.ehr_specialtytemplate t
       WHERE t.id=NEW.template_id AND t.clinic_id=NEW.clinic_id
       AND t.organization_id=NEW.organization_id
       AND t.key='pack_'||NEW.key||'_v'||NEW.version
       AND t.prompts->>'state'='published')
     OR NOT EXISTS (SELECT 1 FROM clinic_app.intake_questionnairetemplate q
       WHERE q.id=NEW.questionnaire_id AND q.clinic_id=NEW.clinic_id
       AND q.organization_id=NEW.organization_id
       AND q.key='pack_'||NEW.key||'_v'||NEW.version) THEN
     RAISE EXCEPTION 'invalid pack binding' USING ERRCODE='23514';
   END IF;
 ELSE
   IF TG_OP<>'INSERT' THEN
     RAISE EXCEPTION 'immutable specialty entry' USING ERRCODE='23514';
   END IF;
   PERFORM pg_advisory_xact_lock_shared(hashtextextended(
     'ehr-pack-use:'||NEW.pack_id::text,0));
   SELECT * INTO p FROM clinic_app.ehr_specialtypack WHERE id=NEW.pack_id;
   SELECT * INTO e FROM clinic_app.ehr_encounter WHERE id=NEW.encounter_id;
   IF p.id IS NULL OR e.id IS NULL OR p.uninstalled_at IS NOT NULL
     OR e.state<>'open'
     OR (p.organization_id,p.clinic_id) IS DISTINCT FROM
        (NEW.organization_id,NEW.clinic_id)
     OR (e.organization_id,e.clinic_id) IS DISTINCT FROM
        (NEW.organization_id,NEW.clinic_id)
     OR e.physician_id<>NEW.recorded_by_id
     OR (NEW.kind='immunization' AND (p.key<>'pediatrics'
       OR NEW.consent_document_id IS NOT NULL OR NEW.images<>'[]'::jsonb))
     OR (NEW.kind='procedure' AND (p.key<>'procedures'
       OR NOT EXISTS (SELECT 1 FROM clinic_app.ehr_clinicalattachment a
         WHERE a.id=NEW.consent_document_id AND a.encounter_id=e.id
         AND a.organization_id=NEW.organization_id AND a.state='available')))
     OR jsonb_typeof(NEW.images) IS DISTINCT FROM 'array'
     OR jsonb_array_length(NEW.images)>20
     OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(NEW.images) image_id
       WHERE NOT EXISTS (SELECT 1 FROM clinic_app.ehr_clinicalattachment a
         WHERE a.id::text=image_id AND a.encounter_id=e.id
         AND a.organization_id=NEW.organization_id AND a.state='available'
         AND a.detected_type IN ('image/jpeg','image/png'))) THEN
     RAISE EXCEPTION 'invalid specialty entry binding' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.ehr_pack_use_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE p clinic_app.ehr_specialtypack;
BEGIN
 IF TG_TABLE_NAME='ehr_clinicaldocumentversion' THEN
   IF NEW.version<>1 THEN RETURN NEW; END IF;
   SELECT * INTO p FROM clinic_app.ehr_specialtypack
    WHERE template_id=NEW.template_id;
   IF p.key='psychiatry' AND NOT EXISTS (
     SELECT 1 FROM clinic_app.ehr_restrictednote n
      WHERE n.document_id=NEW.document_id AND n.clinic_id=p.clinic_id
      AND n.organization_id=NEW.organization_id
      AND n.restricted_by_id=NEW.author_id) THEN
     RAISE EXCEPTION 'psychiatry requires a restricted note' USING ERRCODE='23514';
   END IF;
 ELSE
   SELECT * INTO p FROM clinic_app.ehr_specialtypack
    WHERE questionnaire_id=NEW.template_id;
 END IF;
 IF p.id IS NOT NULL THEN
   PERFORM pg_advisory_xact_lock_shared(hashtextextended(
     'ehr-pack-use:'||p.id::text,0));
   SELECT * INTO p FROM clinic_app.ehr_specialtypack WHERE id=p.id;
 END IF;
 IF p.id IS NOT NULL AND p.uninstalled_at IS NOT NULL THEN
   RAISE EXCEPTION 'specialty pack unavailable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_pack_guard(),
 clinic_app.ehr_pack_use_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_pack_guard(),
 clinic_app.ehr_pack_use_guard(), clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
"""

for table in ("specialtypack", "specialtyentry"):
    SQL += f"""
ALTER TABLE clinic_app.ehr_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.ehr_{table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.ehr_{table} FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT,INSERT ON clinic_app.ehr_{table} TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.ehr_{table} TO clinic_owner
 USING (organization_id=__TENANT__) WITH CHECK (organization_id=__TENANT__);
CREATE TRIGGER specialty_binding BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.ehr_{table} FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_pack_guard();
CREATE TRIGGER specialty_no_truncate BEFORE TRUNCATE ON clinic_app.ehr_{table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.questionnaire_immutable();
"""

SQL += """
GRANT UPDATE(uninstalled_at,position) ON clinic_app.ehr_specialtypack TO clinic_app;
CREATE POLICY pack_read ON clinic_app.ehr_specialtypack FOR SELECT TO clinic_app
 USING (organization_id=__TENANT__ AND clinic_app.questionnaire_staff(
   clinic_id,ARRAY['owner','clinic_admin','physician']));
CREATE POLICY pack_insert ON clinic_app.ehr_specialtypack FOR INSERT TO clinic_app
 WITH CHECK (organization_id=__TENANT__ AND clinical_owner_id=__ACTOR__
 AND clinic_app.questionnaire_staff(clinic_id,ARRAY['owner','clinic_admin']));
CREATE POLICY pack_retire ON clinic_app.ehr_specialtypack FOR UPDATE TO clinic_app
 USING (organization_id=__TENANT__
 AND clinic_app.questionnaire_staff(clinic_id,ARRAY['owner','clinic_admin']))
 WITH CHECK (organization_id=__TENANT__
 AND clinic_app.questionnaire_staff(clinic_id,ARRAY['owner','clinic_admin']));
CREATE POLICY specialty_read ON clinic_app.ehr_specialtyentry FOR SELECT TO clinic_app
 USING (organization_id=__TENANT__ AND
 (clinic_app.ehr_assigned(encounter_id) OR clinic_app.ehr_care(encounter_id))
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_encounter e WHERE e.id=encounter_id
 AND clinic_app.ehr_patient_permission('clinical.read',e.clinic_id,e.patient_id)));
CREATE POLICY specialty_insert ON clinic_app.ehr_specialtyentry FOR INSERT TO clinic_app
 WITH CHECK (organization_id=__TENANT__ AND recorded_by_id=__ACTOR__
 AND clinic_app.ehr_assigned(encounter_id)
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_encounter e WHERE e.id=encounter_id
 AND clinic_app.ehr_patient_permission('clinical.write',e.clinic_id,e.patient_id)));
CREATE TRIGGER pack_note_use BEFORE INSERT
 ON clinic_app.ehr_clinicaldocumentversion
 FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_pack_use_guard();
CREATE TRIGGER pack_questionnaire_use BEFORE INSERT
 ON clinic_app.intake_questionnaireresponse
 FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_pack_use_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ehr_pack_guard(),
 clinic_app.ehr_pack_use_guard(), clinic_app.questionnaire_immutable()
 FROM clinic_owner;
RESET ROLE;
"""
SQL = SQL.replace(
    "__TENANT__", "NULLIF(current_setting('app.current_tenant',true),'')::uuid"
).replace("__ACTOR__", "NULLIF(current_setting('app.current_user_id',true),'')::uuid")
