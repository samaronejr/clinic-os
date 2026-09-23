"""Clinical row predicates, immutable identity bindings and least-privilege grants."""

TABLES = (
    "specialtytemplate",
    "encounter",
    "clinicaldocument",
    "clinicaldocumentversion",
    "encounterintakereference",
)

_sql_parts = [
    """
GRANT SELECT ON clinic_app.ehr_specialtytemplate, clinic_app.ehr_encounter,
 clinic_app.ehr_clinicaldocument, clinic_app.ehr_clinicaldocumentversion,
 clinic_app.ehr_encounterintakereference, clinic_app.intake_questionnaireevent
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ehr_assigned(requested_encounter uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.ehr_encounter e
 JOIN clinic_app.scheduling_appointment a ON a.id = e.appointment_id
 WHERE e.id = requested_encounter
 AND e.organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND e.physician_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND a.practitioner_id = e.physician_id
 AND clinic_app.questionnaire_staff(e.clinic_id, ARRAY['physician']))
$f$;
CREATE FUNCTION clinic_app.ehr_care(requested_encounter uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.ehr_encounter e
 WHERE e.id = requested_encounter
 AND e.organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(e.clinic_id, ARRAY['physician'])
 AND (EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
   WHERE a.clinic_id = e.clinic_id AND a.patient_id = e.patient_id
   AND a.practitioner_id =
     NULLIF(current_setting('app.current_user_id', true), '')::uuid)
 OR EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
   JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
   JOIN clinic_app.ehr_encounter other ON other.id = d.encounter_id
   WHERE other.clinic_id = e.clinic_id AND other.patient_id = e.patient_id
   AND v.author_id =
     NULLIF(current_setting('app.current_user_id', true), '')::uuid)))
$f$;
CREATE FUNCTION clinic_app.ehr_version_scope(
 requested_clinic uuid, requested_version uuid)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT e.appointment_id FROM clinic_app.ehr_clinicaldocumentversion v
 JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
 JOIN clinic_app.ehr_encounter e ON e.id = d.encounter_id
 WHERE v.id = requested_version AND e.clinic_id = requested_clinic
 AND e.organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(e.clinic_id,
   ARRAY['physician','receptionist','owner','clinic_admin'])
$f$;
CREATE FUNCTION clinic_app.ehr_binding_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 encounter_row clinic_app.ehr_encounter;
 document_row clinic_app.ehr_clinicaldocument;
BEGIN
 IF TG_TABLE_NAME = 'ehr_encounter' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
     JOIN clinic_app.intake_patientclinicenrollment p
       ON p.patient_id = a.patient_id AND p.clinic_id = a.clinic_id
       AND p.organization_id = a.organization_id
     WHERE a.id = NEW.appointment_id AND a.organization_id = NEW.organization_id
       AND a.clinic_id = NEW.clinic_id AND a.patient_id = NEW.patient_id
       AND a.practitioner_id = NEW.physician_id AND a.status = 'scheduled')
     OR NEW.state <> 'open' OR NEW.revision <> 1 THEN
      RAISE EXCEPTION 'invalid encounter binding' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_specialtytemplate' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
      WHERE c.id = NEW.clinic_id AND c.organization_id = NEW.organization_id)
      OR jsonb_typeof(NEW.prompts) <> 'object'
      OR NOT (NEW.prompts ?& ARRAY['subjective','objective','assessment','plan'])
      OR (NEW.prompts - ARRAY['subjective','objective','assessment','plan'])
         <> '{}'::jsonb THEN
     RAISE EXCEPTION 'invalid template binding' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_clinicaldocument' THEN
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = NEW.encounter_id;
   IF encounter_row.id IS NULL
      OR encounter_row.organization_id <> NEW.organization_id
      OR encounter_row.state <> 'open' OR NEW.kind <> 'soap' THEN
     RAISE EXCEPTION 'invalid document binding' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_clinicaldocumentversion' THEN
   SELECT * INTO document_row FROM clinic_app.ehr_clinicaldocument
     WHERE id = NEW.document_id;
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = document_row.encounter_id;
   IF document_row.id IS NULL OR document_row.organization_id <> NEW.organization_id
      OR encounter_row.physician_id <> NEW.author_id
      OR NOT EXISTS (SELECT 1 FROM clinic_app.ehr_specialtytemplate t
         WHERE t.id = NEW.template_id AND t.clinic_id = encounter_row.clinic_id
         AND t.organization_id = NEW.organization_id) THEN
     RAISE EXCEPTION 'invalid version binding' USING ERRCODE = '23514';
   END IF;
   IF TG_OP = 'INSERT' THEN
     IF encounter_row.state <> 'open' OR NEW.state <> 'draft' OR NEW.version <> 1
        OR NEW.revision <> 1 OR NEW.subjective <> '' OR NEW.objective <> ''
        OR NEW.assessment <> '' OR NEW.plan <> '' THEN
       RAISE EXCEPTION 'invalid initial draft' USING ERRCODE = '23514';
     END IF;
   ELSE
     IF (NEW.id,NEW.organization_id,NEW.document_id,NEW.template_id,NEW.author_id,
         NEW.version,NEW.state,NEW.created_at) IS DISTINCT FROM
        (OLD.id,OLD.organization_id,OLD.document_id,OLD.template_id,OLD.author_id,
         OLD.version,OLD.state,OLD.created_at)
        OR OLD.state <> 'draft' OR NEW.revision <> OLD.revision + 1 THEN
       RAISE EXCEPTION 'immutable version or stale revision' USING ERRCODE = '23514';
     END IF;
   END IF;
   IF greatest(length(NEW.subjective),length(NEW.objective),
      length(NEW.assessment),length(NEW.plan)) > 20000 THEN
     RAISE EXCEPTION 'SOAP content limit' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_encounterintakereference' THEN
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = NEW.encounter_id;
   IF encounter_row.id IS NULL
      OR encounter_row.organization_id <> NEW.organization_id
      OR NOT EXISTS (SELECT 1 FROM clinic_app.intake_questionnaireevent q
       JOIN clinic_app.intake_questionnaireresponse r ON r.id = q.response_id
       WHERE q.id = NEW.submission_id AND q.action = 'submitted'
       AND q.organization_id = NEW.organization_id
       AND q.clinic_id = encounter_row.clinic_id
       AND r.patient_id = encounter_row.patient_id
       AND (r.appointment_id IS NULL
         OR r.appointment_id = encounter_row.appointment_id)) THEN
     RAISE EXCEPTION 'invalid intake reference' USING ERRCODE = '23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_assigned(uuid), clinic_app.ehr_care(uuid),
 clinic_app.ehr_version_scope(uuid,uuid), clinic_app.ehr_binding_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_assigned(uuid), clinic_app.ehr_care(uuid),
 clinic_app.ehr_version_scope(uuid,uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_binding_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
"""
]

for table in TABLES:
    binding_events = (
        "INSERT OR UPDATE" if table == "clinicaldocumentversion" else "INSERT"
    )
    immutable_events = (
        "DELETE" if table == "clinicaldocumentversion" else "UPDATE OR DELETE"
    )
    _sql_parts.append(f"""
ALTER TABLE clinic_app.ehr_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.ehr_{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.ehr_{table} TO clinic_owner
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid)
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid);
REVOKE ALL ON clinic_app.ehr_{table} FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.ehr_{table} TO clinic_app;
CREATE TRIGGER ehr_binding BEFORE {binding_events}
 ON clinic_app.ehr_{table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ehr_binding_guard();
CREATE TRIGGER ehr_immutable BEFORE {immutable_events}
 ON clinic_app.ehr_{table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
""")

_sql_parts.append("""
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ehr_binding_guard(),
 clinic_app.questionnaire_immutable() FROM clinic_owner;
RESET ROLE;
GRANT UPDATE (subjective,objective,assessment,plan,revision,updated_at)
 ON clinic_app.ehr_clinicaldocumentversion TO clinic_app;
-- Parent locking requires UPDATE privilege/policy; the trigger forbids mutation.
GRANT UPDATE (revision) ON clinic_app.ehr_encounter TO clinic_app;
CREATE POLICY clinical_read ON clinic_app.ehr_specialtytemplate
 FOR SELECT TO clinic_app
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,
   ARRAY['physician','clinic_admin','owner']));
CREATE POLICY clinical_insert ON clinic_app.ehr_specialtytemplate
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['clinic_admin','owner']));
CREATE POLICY clinical_read ON clinic_app.ehr_encounter FOR SELECT TO clinic_app
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,
   ARRAY['physician','receptionist','owner','clinic_admin']));
CREATE POLICY clinical_insert ON clinic_app.ehr_encounter FOR INSERT TO clinic_app
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND physician_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['physician']));
CREATE POLICY clinical_lock ON clinic_app.ehr_encounter FOR UPDATE TO clinic_app
 USING (clinic_app.ehr_assigned(id)) WITH CHECK (clinic_app.ehr_assigned(id));
CREATE POLICY clinical_read ON clinic_app.ehr_clinicaldocument
 FOR SELECT TO clinic_app
 USING (EXISTS (SELECT 1 FROM clinic_app.ehr_encounter e WHERE e.id = encounter_id
 AND e.organization_id = ehr_clinicaldocument.organization_id));
CREATE POLICY clinical_insert ON clinic_app.ehr_clinicaldocument
 FOR INSERT TO clinic_app WITH CHECK (clinic_app.ehr_assigned(encounter_id));
CREATE POLICY clinical_read ON clinic_app.ehr_clinicaldocumentversion
 FOR SELECT TO clinic_app
 USING (state <> 'discarded' AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_clinicaldocument d WHERE d.id = document_id
 AND d.organization_id = ehr_clinicaldocumentversion.organization_id
 AND ((clinic_app.ehr_assigned(d.encounter_id) AND
   (state <> 'draft' OR author_id =
     NULLIF(current_setting('app.current_user_id', true), '')::uuid))
 OR (state IN ('finalized','superseded') AND clinic_app.ehr_care(d.encounter_id)))));
CREATE POLICY clinical_insert ON clinic_app.ehr_clinicaldocumentversion
 FOR INSERT TO clinic_app
 WITH CHECK (author_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d WHERE d.id = document_id
 AND clinic_app.ehr_assigned(d.encounter_id)));
CREATE POLICY clinical_update ON clinic_app.ehr_clinicaldocumentversion
 FOR UPDATE TO clinic_app
 USING (state = 'draft' AND author_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d WHERE d.id = document_id
 AND clinic_app.ehr_assigned(d.encounter_id)))
 WITH CHECK (state = 'draft' AND author_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d WHERE d.id = document_id
 AND clinic_app.ehr_assigned(d.encounter_id)));
CREATE POLICY clinical_read ON clinic_app.ehr_encounterintakereference
 FOR SELECT TO clinic_app USING (clinic_app.ehr_assigned(encounter_id));
CREATE POLICY clinical_insert ON clinic_app.ehr_encounterintakereference
 FOR INSERT TO clinic_app WITH CHECK (clinic_app.ehr_assigned(encounter_id));
""")
SQL = "".join(_sql_parts)

_reverse_parts = [
    f"""
DROP TRIGGER ehr_binding ON clinic_app.ehr_{table};
DROP TRIGGER ehr_immutable ON clinic_app.ehr_{table};
DROP POLICY clinical_read ON clinic_app.ehr_{table};
DROP POLICY clinical_insert ON clinic_app.ehr_{table};
"""
    for table in reversed(TABLES)
]
_reverse_parts.append("""
DROP POLICY clinical_update ON clinic_app.ehr_clinicaldocumentversion;
DROP POLICY clinical_lock ON clinic_app.ehr_encounter;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ehr_binding_guard();
DROP FUNCTION clinic_app.ehr_version_scope(uuid,uuid);
DROP FUNCTION clinic_app.ehr_care(uuid);
DROP FUNCTION clinic_app.ehr_assigned(uuid);
RESET ROLE;
""")
REVERSE_SQL = "".join(_reverse_parts)
