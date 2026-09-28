"""Plan item 27: addendum drafts for clinicians other than the draft's author.

Authority is ``has_permission('clinical.write', clinic, enrollment)`` for the
encounter's patient in the encounter's clinic, and never the encounter's own
assigned physician (who writes the main draft). The guard binds every row to
the encounter's organization, clinic and patient, requires an open encounter,
keeps identity columns immutable and moves revisions forward by exactly one.
The guard reads no actor setting; the policies decide who may write.
"""

from apps.ehr.migrations._autosave_sql import _render

TABLES = ("ehr_encounteraddendum", "ehr_addendumsavereceipt")

_GUARD = """
CREATE FUNCTION clinic_app.ehr_addendum_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 encounter_row clinic_app.ehr_encounter;
 addendum_row clinic_app.ehr_encounteraddendum;
BEGIN
 IF TG_TABLE_NAME = 'ehr_encounteraddendum' THEN
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = NEW.encounter_id;
   IF encounter_row.id IS NULL
      OR encounter_row.organization_id <> NEW.organization_id
      OR encounter_row.clinic_id <> NEW.clinic_id
      OR encounter_row.patient_id <> NEW.patient_id
      OR encounter_row.physician_id = NEW.author_id
      OR encounter_row.state <> 'open' THEN
     RAISE EXCEPTION 'invalid addendum binding' USING ERRCODE = '23514';
   END IF;
   IF TG_OP = 'INSERT' THEN
     IF NEW.state <> 'draft' OR NEW.revision <> 1
        OR NEW.text IS NOT NULL OR NEW.text_sha256 <> '' THEN
       RAISE EXCEPTION 'invalid initial addendum' USING ERRCODE = '23514';
     END IF;
   ELSIF (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.encounter_id,
          NEW.patient_id,NEW.author_id,NEW.state,NEW.created_at)
         IS DISTINCT FROM
         (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.encounter_id,
          OLD.patient_id,OLD.author_id,OLD.state,OLD.created_at)
      OR OLD.state <> 'draft' OR NEW.revision <> OLD.revision + 1
      OR NEW.text_sha256 !~ '^[0-9a-f]{64}$'
      OR (NEW.text IS DISTINCT FROM OLD.text)
         <> (NEW.text_sha256 IS DISTINCT FROM OLD.text_sha256) THEN
     RAISE EXCEPTION 'immutable addendum or stale revision'
       USING ERRCODE = '23514';
   END IF;
   IF NEW.text IS NOT NULL AND pg_catalog.octet_length(NEW.text) > 120000 THEN
     RAISE EXCEPTION 'addendum content limit' USING ERRCODE = '23514';
   END IF;
 ELSE
   SELECT * INTO addendum_row FROM clinic_app.ehr_encounteraddendum
     WHERE id = NEW.addendum_id;
   -- A receipt acknowledges the revision the same transaction committed.
   IF addendum_row.id IS NULL
      OR addendum_row.organization_id <> NEW.organization_id
      OR addendum_row.state <> 'draft'
      OR NEW.revision <> addendum_row.revision
      OR NEW.request_sha256 !~ '^[0-9a-f]{64}$' THEN
     RAISE EXCEPTION 'invalid addendum receipt' USING ERRCODE = '23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_addendum_guard() FROM PUBLIC;
"""

_ENROLLMENT = """(SELECT p.id FROM clinic_app.intake_patientclinicenrollment p
 WHERE p.clinic_id = ehr_encounteraddendum.clinic_id
 AND p.patient_id = ehr_encounteraddendum.patient_id
 AND p.organization_id = ehr_encounteraddendum.organization_id)"""

_POLICIES = """
CREATE POLICY addendum_read ON clinic_app.ehr_encounteraddendum
 FOR SELECT TO clinic_app
 USING (organization_id = __TENANT__
 AND author_id = __ACTOR__
 AND clinic_app.has_permission('clinical.read', clinic_id, __ENROLLMENT__));
CREATE POLICY addendum_insert ON clinic_app.ehr_encounteraddendum
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id = __TENANT__
 AND author_id = __ACTOR__
 AND clinic_app.has_permission('clinical.write', clinic_id, __ENROLLMENT__)
 AND NOT clinic_app.ehr_assigned(encounter_id));
CREATE POLICY addendum_write ON clinic_app.ehr_encounteraddendum
 FOR UPDATE TO clinic_app
 USING (state = 'draft' AND author_id = __ACTOR__
 AND clinic_app.has_permission('clinical.write', clinic_id, __ENROLLMENT__))
 WITH CHECK (state = 'draft' AND author_id = __ACTOR__
 AND clinic_app.has_permission('clinical.write', clinic_id, __ENROLLMENT__));
CREATE POLICY addendum_receipt_read ON clinic_app.ehr_addendumsavereceipt
 FOR SELECT TO clinic_app
 USING (organization_id = __TENANT__ AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_encounteraddendum a
 WHERE a.id = addendum_id AND a.state = 'draft'));
CREATE POLICY addendum_receipt_insert ON clinic_app.ehr_addendumsavereceipt
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id = __TENANT__ AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_encounteraddendum a
 WHERE a.id = addendum_id AND a.state = 'draft'));
""".replace("__ENROLLMENT__", _ENROLLMENT)


def _posture(table: str, grants: str) -> str:
    return (
        """
ALTER TABLE clinic_app.__TABLE__ ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.__TABLE__ FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.__TABLE__ TO clinic_owner
 USING (organization_id = __TENANT__)
 WITH CHECK (organization_id = __TENANT__);
REVOKE ALL ON clinic_app.__TABLE__ FROM PUBLIC, clinic_app;
GRANT __GRANTS__ ON clinic_app.__TABLE__ TO clinic_app;
CREATE TRIGGER ehr_addendum_binding BEFORE __EVENTS__
 ON clinic_app.__TABLE__ FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ehr_addendum_guard();
CREATE TRIGGER ehr_addendum_immutable BEFORE __IMMUTABLE__
 ON clinic_app.__TABLE__ FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
""".replace("__TABLE__", table)
        .replace("__GRANTS__", grants)
        .replace(
            "__EVENTS__",
            "INSERT OR UPDATE" if table == "ehr_encounteraddendum" else "INSERT",
        )
        .replace(
            "__IMMUTABLE__",
            "DELETE" if table == "ehr_encounteraddendum" else "UPDATE OR DELETE",
        )
    )


# The guard reads the addendum while binding receipts.
_RESOLVER_READS = (
    "GRANT SELECT ON clinic_app.ehr_encounteraddendum TO clinic_resolver;\n"
)

SQL = _render(
    _RESOLVER_READS
    + "SET LOCAL ROLE clinic_resolver;\n"
    + _GUARD
    + "GRANT EXECUTE ON FUNCTION clinic_app.ehr_addendum_guard(),\n"
    " clinic_app.questionnaire_immutable() TO clinic_owner;\n"
    "RESET ROLE;\n"
    + _posture("ehr_encounteraddendum", "SELECT, INSERT")
    + _posture("ehr_addendumsavereceipt", "SELECT, INSERT")
    + "GRANT UPDATE (text, text_sha256, revision, updated_at)\n"
    " ON clinic_app.ehr_encounteraddendum TO clinic_app;\n"
    + _POLICIES
    + "SET LOCAL ROLE clinic_resolver;\n"
    "REVOKE EXECUTE ON FUNCTION clinic_app.ehr_addendum_guard(),\n"
    " clinic_app.questionnaire_immutable() FROM clinic_owner;\n"
    "RESET ROLE;\n"
)

# The model operations drop both tables (and their policies) afterwards.
REVERSE_SQL = (
    "".join(
        f"DROP TRIGGER ehr_addendum_binding ON clinic_app.{table};\n"
        f"DROP TRIGGER ehr_addendum_immutable ON clinic_app.{table};\n"
        for table in TABLES
    )
    + "SET LOCAL ROLE clinic_resolver;\n"
    "DROP FUNCTION clinic_app.ehr_addendum_guard();\n"
    "RESET ROLE;\n"
)
