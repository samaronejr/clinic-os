"""Independent database scope, immutable snapshots and encounter lifecycle guards."""

_parts = [
    """
GRANT SELECT ON clinic_app.prescription_prescriptiondraft TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.prescription_draft_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE e clinic_app.ehr_encounter;
BEGIN
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
       NEW.issuer_id,NEW.category,NEW.contract_version,NEW.created_at)
      IS DISTINCT FROM
      (OLD.id,OLD.organization_id,OLD.encounter_id,OLD.clinic_id,OLD.patient_id,
       OLD.issuer_id,OLD.category,OLD.contract_version,OLD.created_at)
      OR OLD.state <> 'draft' OR NEW.version <> OLD.version + 1
      OR e.state <> 'open' THEN
     RAISE EXCEPTION 'immutable prescription or stale version' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.prescription_item_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM clinic_app.prescription_prescriptiondraft d
   WHERE d.id=NEW.draft_id AND d.organization_id=NEW.organization_id
     AND d.version=NEW.version AND d.state='draft')
   OR btrim(NEW.medication_description)='' OR btrim(NEW.strength_form)=''
   OR btrim(NEW.dose)='' OR btrim(NEW.route)='' OR btrim(NEW.frequency)=''
   OR btrim(NEW.duration)='' OR btrim(NEW.quantity)='' THEN
   RAISE EXCEPTION 'invalid prescription item binding' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.prescription_close_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF NEW.state='closed' AND EXISTS (
   SELECT 1 FROM clinic_app.prescription_prescriptiondraft
   WHERE encounter_id=NEW.id AND state='draft') THEN
   RAISE EXCEPTION 'draft_in_progress' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.prescription_draft_guard(),
 clinic_app.prescription_item_guard(), clinic_app.prescription_close_guard()
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.prescription_draft_guard(),
 clinic_app.prescription_item_guard(), clinic_app.prescription_close_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
"""
]

_parts.extend(
    f"""
ALTER TABLE clinic_app.prescription_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.prescription_{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.prescription_{table} TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
REVOKE ALL ON clinic_app.prescription_{table} FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.prescription_{table} TO clinic_app;
"""
    for table in ("prescriptiondraft", "prescriptionitem")
)

_parts.append("""
GRANT UPDATE(version,state,updated_at)
 ON clinic_app.prescription_prescriptiondraft TO clinic_app;
CREATE POLICY draft_read ON clinic_app.prescription_prescriptiondraft
 FOR SELECT TO clinic_app
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE POLICY draft_insert ON clinic_app.prescription_prescriptiondraft
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE POLICY draft_update ON clinic_app.prescription_prescriptiondraft
 FOR UPDATE TO clinic_app
 USING (state='draft' AND organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id))
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND issuer_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE POLICY item_read ON clinic_app.prescription_prescriptionitem
 FOR SELECT TO clinic_app
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.prescription_prescriptiondraft d
   WHERE d.id=draft_id AND d.state='draft'));
CREATE POLICY item_insert ON clinic_app.prescription_prescriptionitem
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.prescription_prescriptiondraft d
   WHERE d.id=draft_id AND d.state='draft'
   AND d.version=prescription_prescriptionitem.version));
CREATE TRIGGER prescription_draft_binding BEFORE INSERT OR UPDATE
 ON clinic_app.prescription_prescriptiondraft FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_draft_guard();
CREATE TRIGGER prescription_draft_immutable BEFORE DELETE
 ON clinic_app.prescription_prescriptiondraft FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER prescription_item_binding BEFORE INSERT
 ON clinic_app.prescription_prescriptionitem FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_item_guard();
CREATE TRIGGER prescription_item_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.prescription_prescriptionitem FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER prescription_encounter_close BEFORE UPDATE ON clinic_app.ehr_encounter
 FOR EACH ROW EXECUTE FUNCTION clinic_app.prescription_close_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.prescription_draft_guard(),
 clinic_app.prescription_item_guard(), clinic_app.prescription_close_guard(),
 clinic_app.questionnaire_immutable() FROM clinic_owner;
RESET ROLE;
""")
SQL = "".join(_parts)

REVERSE_SQL = """
DROP TRIGGER prescription_encounter_close ON clinic_app.ehr_encounter;
DROP TRIGGER prescription_draft_binding ON clinic_app.prescription_prescriptiondraft;
DROP TRIGGER prescription_draft_immutable ON clinic_app.prescription_prescriptiondraft;
DROP TRIGGER prescription_item_binding ON clinic_app.prescription_prescriptionitem;
DROP TRIGGER prescription_item_immutable ON clinic_app.prescription_prescriptionitem;
DROP POLICY draft_read ON clinic_app.prescription_prescriptiondraft;
DROP POLICY draft_insert ON clinic_app.prescription_prescriptiondraft;
DROP POLICY draft_update ON clinic_app.prescription_prescriptiondraft;
DROP POLICY item_read ON clinic_app.prescription_prescriptionitem;
DROP POLICY item_insert ON clinic_app.prescription_prescriptionitem;
DROP POLICY setup_tenant ON clinic_app.prescription_prescriptiondraft;
DROP POLICY setup_tenant ON clinic_app.prescription_prescriptionitem;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.prescription_draft_guard();
DROP FUNCTION clinic_app.prescription_item_guard();
DROP FUNCTION clinic_app.prescription_close_guard();
RESET ROLE;
"""
