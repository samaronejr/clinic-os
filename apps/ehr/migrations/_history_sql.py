"""Append-only clinical history, canonical care predicates and serialized revisions."""

TABLES = ("historyassessment", "problem", "allergy")

_sql = """
GRANT SELECT (id, username) ON clinic_app.identity_user TO clinic_resolver;
GRANT SELECT ON clinic_app.ehr_historyassessment, clinic_app.ehr_problem,
 clinic_app.ehr_allergy TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ehr_history_care(requested_encounter uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT clinic_app.ehr_care(requested_encounter) OR EXISTS (
 SELECT 1 FROM clinic_app.ehr_encounter e
 JOIN clinic_app.ehr_historyassessment h ON h.clinic_id=e.clinic_id
   AND h.patient_id=e.patient_id AND h.organization_id=e.organization_id
 WHERE e.id=requested_encounter
 AND e.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.questionnaire_staff(e.clinic_id, ARRAY['physician'])
 AND h.author_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid)
$f$;
CREATE FUNCTION clinic_app.ehr_history_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 e clinic_app.ehr_encounter;
 h clinic_app.ehr_historyassessment;
 previous record;
 latest integer;
 category text;
BEGIN
 IF TG_TABLE_NAME='ehr_historyassessment' THEN
   SELECT * INTO e FROM clinic_app.ehr_encounter WHERE id=NEW.encounter_id;
   IF e.id IS NULL OR (e.organization_id,e.clinic_id,e.patient_id,e.physician_id)
     IS DISTINCT FROM (NEW.organization_id,NEW.clinic_id,NEW.patient_id,NEW.author_id)
     OR NOT clinic_app.ehr_assigned(e.id) OR btrim(NEW.reason)='' THEN
     RAISE EXCEPTION 'invalid history authority or binding' USING ERRCODE='23514';
   END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended(
      'ehr-history:'||NEW.clinic_id||':'||NEW.patient_id||':'||NEW.kind,0));
   SELECT coalesce(max(revision),0) INTO latest FROM clinic_app.ehr_historyassessment
     WHERE clinic_id=NEW.clinic_id AND patient_id=NEW.patient_id AND kind=NEW.kind;
   IF NEW.revision<>latest+1 THEN
     RAISE EXCEPTION 'stale history revision' USING ERRCODE='23514';
   END IF;
   IF NEW.state<>'documented' AND EXISTS (
       SELECT 1 FROM clinic_app.ehr_historyassessment
       WHERE clinic_id=NEW.clinic_id AND patient_id=NEW.patient_id
         AND kind=NEW.kind AND state='documented') THEN
     RAISE EXCEPTION 'documented history cannot imply absence' USING ERRCODE='23514';
   END IF;
   SELECT username INTO NEW.author_label FROM clinic_app.identity_user
      WHERE id=NEW.author_id;
   NEW.created_at=clock_timestamp();
 ELSE
   SELECT * INTO h FROM clinic_app.ehr_historyassessment WHERE id=NEW.assessment_id;
   category=CASE WHEN TG_TABLE_NAME='ehr_problem' THEN 'problem' ELSE 'allergy' END;
   IF h.id IS NULL OR h.organization_id<>NEW.organization_id
      OR h.kind<>category OR h.state<>'documented'
      OR NOT clinic_app.ehr_assigned(h.encounter_id)
      OR btrim(NEW.description)='' THEN
     RAISE EXCEPTION 'invalid history entry binding' USING ERRCODE='23514';
   END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended(
      'ehr-history:'||h.clinic_id||':'||h.patient_id||':'||h.kind,0));
   SELECT max(revision) INTO latest FROM clinic_app.ehr_historyassessment
     WHERE clinic_id=h.clinic_id AND patient_id=h.patient_id AND kind=h.kind;
   IF latest<>h.revision THEN
     RAISE EXCEPTION 'stale assessment binding' USING ERRCODE='23514';
   END IF;
   IF category='problem' THEN
     SELECT v.version,ha.clinic_id,ha.patient_id,ha.encounter_id INTO previous
        FROM clinic_app.ehr_problem v JOIN clinic_app.ehr_historyassessment ha
          ON ha.id=v.assessment_id
        WHERE v.entry_id=NEW.entry_id
        ORDER BY v.version DESC LIMIT 1;
   ELSE
     SELECT v.version,ha.clinic_id,ha.patient_id,ha.encounter_id INTO previous
        FROM clinic_app.ehr_allergy v JOIN clinic_app.ehr_historyassessment ha
          ON ha.id=v.assessment_id
        WHERE v.entry_id=NEW.entry_id
        ORDER BY v.version DESC LIMIT 1;
   END IF;
   IF previous.version IS NULL THEN
     IF NEW.version<>1 THEN
       RAISE EXCEPTION 'invalid initial version' USING ERRCODE='23514';
     END IF;
   ELSIF NEW.version<>previous.version+1
      OR (previous.clinic_id,previous.patient_id)
        IS DISTINCT FROM (h.clinic_id,h.patient_id)
      OR NOT clinic_app.ehr_assigned(previous.encounter_id) THEN
     RAISE EXCEPTION 'invalid predecessor or authority' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_history_care(uuid),
 clinic_app.ehr_history_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_history_care(uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_history_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
"""

for table in TABLES:
    predicate = (
        "clinic_app.ehr_history_care(encounter_id)"
        if table == "historyassessment"
        else (
            "EXISTS (SELECT 1 FROM clinic_app.ehr_historyassessment h "
            "WHERE h.id=assessment_id AND h.organization_id=__TABLE__.organization_id)"
        ).replace("__TABLE__", "ehr_" + table)
    )
    insert_predicate = (
        "clinic_app.ehr_assigned(encounter_id) AND author_id="
        "NULLIF(current_setting('app.current_user_id',true),'')::uuid"
        if table == "historyassessment"
        else "EXISTS (SELECT 1 FROM clinic_app.ehr_historyassessment h "
        "WHERE h.id=assessment_id AND clinic_app.ehr_assigned(h.encounter_id) "
        "AND h.author_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid)"
    )
    _sql += f"""
ALTER TABLE clinic_app.ehr_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.ehr_{table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.ehr_{table} FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.ehr_{table} TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.ehr_{table} TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY history_read ON clinic_app.ehr_{table} FOR SELECT TO clinic_app
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND ({predicate}));
CREATE POLICY history_insert ON clinic_app.ehr_{table} FOR INSERT TO clinic_app
 WITH CHECK (organization_id=
 NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND ({insert_predicate}));
CREATE TRIGGER history_binding BEFORE INSERT ON clinic_app.ehr_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_history_guard();
CREATE TRIGGER history_immutable BEFORE UPDATE OR DELETE ON clinic_app.ehr_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.questionnaire_immutable();
"""
SQL = (
    _sql
    + """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ehr_history_guard(),
 clinic_app.questionnaire_immutable() FROM clinic_owner;
RESET ROLE;
"""
)

REVERSE_SQL = (
    "".join(
        f"""
DROP TRIGGER history_binding ON clinic_app.ehr_{table};
DROP TRIGGER history_immutable ON clinic_app.ehr_{table};
DROP POLICY history_read ON clinic_app.ehr_{table};
DROP POLICY history_insert ON clinic_app.ehr_{table};
DROP POLICY setup_tenant ON clinic_app.ehr_{table};
"""
        for table in reversed(TABLES)
    )
    + """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ehr_history_guard();
DROP FUNCTION clinic_app.ehr_history_care(uuid);
RESET ROLE;
"""
)
