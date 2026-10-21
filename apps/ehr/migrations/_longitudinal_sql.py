"""Longitudinal facts: exact scope, provenance transitions and least privilege."""

TENANT = "NULLIF(current_setting('app.current_tenant',true),'')::uuid"
ACTOR = "NULLIF(current_setting('app.current_user_id',true),'')::uuid"
TABLES = (
    "ehr_medicationstatement",
    "ehr_observation",
    "ehr_restrictednote",
    "ehr_noteaccessgrant",
    "ehr_copiednoteblock",
    "ehr_timelinerefresh",
    "ehr_timelineprojection",
)


def render(sql: str) -> str:
    """Expand only fixed migration-owned tokens."""
    return sql.replace("__TENANT__", TENANT).replace("__ACTOR__", ACTOR)


POSTURE = "".join(
    f"""
ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.{table} FROM PUBLIC, clinic_app, clinic_agent;
CREATE POLICY setup_tenant ON clinic_app.{table} TO clinic_owner
 USING (organization_id = {TENANT}) WITH CHECK (organization_id = {TENANT});
CREATE TRIGGER ehr_longitudinal_no_delete BEFORE DELETE
 ON clinic_app.{table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
"""
    for table in TABLES
)

FUNCTIONS = """
GRANT SELECT ON clinic_app.ehr_medicationstatement, clinic_app.ehr_observation,
 clinic_app.ehr_restrictednote, clinic_app.ehr_noteaccessgrant,
 clinic_app.ehr_copiednoteblock, clinic_app.ehr_timelinerefresh,
 clinic_app.ehr_timelineprojection, clinic_app.ehr_episodeencounter
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ehr_patient_permission(
 permission text, requested_clinic uuid, requested_patient uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment p
 WHERE p.organization_id=__TENANT__ AND p.clinic_id=requested_clinic
 AND p.patient_id=requested_patient
 AND clinic_app.has_permission(permission, requested_clinic, p.id))
$f$;
CREATE FUNCTION clinic_app.ehr_longitudinal_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE allowed text[]; stamp timestamptz; reviewer uuid;
BEGIN
 IF NOT EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment p
 WHERE p.organization_id=NEW.organization_id AND p.clinic_id=NEW.clinic_id
 AND p.patient_id=NEW.patient_id) THEN
   RAISE EXCEPTION 'invalid clinical scope' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='ehr_medicationstatement' THEN
   stamp:=NEW.reconciled_at; reviewer:=NEW.reconciled_by_id;
   allowed:=ARRAY['status','provenance','revision','reconciled_at','reconciled_by_id'];
   IF NEW.details IS NULL OR octet_length(NEW.details)>30000 THEN
     RAISE EXCEPTION 'invalid medication envelope' USING ERRCODE='23514';
   END IF;
 ELSE
   stamp:=NEW.confirmed_at; reviewer:=NEW.confirmed_by_id;
   allowed:=ARRAY['provenance','revision','confirmed_at','confirmed_by_id'];
   IF NEW.value IS NULL OR octet_length(NEW.value)>30000
   OR NOT ((NEW.kind IN ('systolic_bp','diastolic_bp') AND NEW.unit='mm[Hg]')
    OR (NEW.kind IN ('heart_rate','respiratory_rate') AND NEW.unit='/min')
    OR (NEW.kind='temperature' AND NEW.unit='Cel')
    OR (NEW.kind='spo2' AND NEW.unit='%')
    OR (NEW.kind='weight' AND NEW.unit='kg')
    OR (NEW.kind='height' AND NEW.unit='cm')
    OR (NEW.kind='bmi' AND NEW.unit='kg/m2')
    OR (NEW.kind='pain_present' AND NEW.unit='1')) THEN
     RAISE EXCEPTION 'invalid observation envelope or unit' USING ERRCODE='23514';
   END IF;
 END IF;
 IF TG_OP='INSERT' THEN
   IF NEW.revision<>1 OR NEW.provenance<>NEW.origin
      OR stamp IS NOT NULL OR reviewer IS NOT NULL THEN
     RAISE EXCEPTION 'confirmation requires review' USING ERRCODE='23514';
   END IF;
 ELSE
   IF (to_jsonb(NEW)-allowed) IS DISTINCT FROM (to_jsonb(OLD)-allowed)
      OR NEW.revision<>OLD.revision+1
      OR NEW.provenance<>'clinician_confirmed'
      OR stamp IS NULL OR reviewer IS NULL OR reviewer<>__ACTOR__
      OR (TG_TABLE_NAME='ehr_observation'
          AND OLD.provenance='clinician_confirmed') THEN
     RAISE EXCEPTION 'invalid provenance transition' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_patient_permission(text,uuid,uuid),
 clinic_app.ehr_longitudinal_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_patient_permission(text,uuid,uuid)
 TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_longitudinal_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
"""


def _fact_policies(table: str, permission: str, read: str) -> str:
    return f"""
GRANT SELECT, INSERT ON clinic_app.{table} TO clinic_app;
CREATE POLICY longitudinal_read ON clinic_app.{table}
 FOR SELECT TO clinic_app USING (organization_id=__TENANT__ AND ({read}));
CREATE POLICY longitudinal_insert ON clinic_app.{table}
 FOR INSERT TO clinic_app WITH CHECK (
 organization_id=__TENANT__ AND performer_id=__ACTOR__
 AND clinic_app.ehr_patient_permission('{permission}',clinic_id,patient_id));
CREATE POLICY longitudinal_confirm ON clinic_app.{table}
 FOR UPDATE TO clinic_app USING (organization_id=__TENANT__
 AND clinic_app.ehr_patient_permission('clinical.write',clinic_id,patient_id))
 WITH CHECK (organization_id=__TENANT__
 AND clinic_app.ehr_patient_permission('clinical.write',clinic_id,patient_id));
CREATE TRIGGER ehr_longitudinal_binding BEFORE INSERT OR UPDATE
 ON clinic_app.{table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ehr_longitudinal_guard();
"""


READ = "clinic_app.ehr_patient_permission('clinical.read',clinic_id,patient_id)"
OBSERVATION_READ = (
    READ
    + " OR clinic_app.ehr_patient_permission('observation.write',clinic_id,patient_id)"
)
POLICIES = (
    _fact_policies("ehr_medicationstatement", "clinical.write", READ)
    + _fact_policies(
        "ehr_observation",
        "clinical.write",  # Physician and nursing inserts are separate OR policies.
        OBSERVATION_READ,
    )
    + """
CREATE POLICY observation_nursing_insert ON clinic_app.ehr_observation
 FOR INSERT TO clinic_app WITH CHECK (organization_id=__TENANT__
 AND performer_id=__ACTOR__
 AND clinic_app.ehr_patient_permission('observation.write',clinic_id,patient_id));
GRANT UPDATE (status,provenance,revision,reconciled_at,reconciled_by_id)
 ON clinic_app.ehr_medicationstatement TO clinic_app;
GRANT UPDATE (provenance,revision,confirmed_at,confirmed_by_id)
 ON clinic_app.ehr_observation TO clinic_app;
"""
)
