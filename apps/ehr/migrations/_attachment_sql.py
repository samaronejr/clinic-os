"""Attachment predicates, transition bindings and least-privilege grants.

Bytes never live in this table; the row binds an opaque storage key to one
encounter and records the quarantine lifecycle. ``clinic_app`` may update only
the scan columns of a quarantined row it is assigned to; every other mutation
and every delete is rejected by trigger.
"""

TABLE = "ehr_clinicalattachment"

_sql_parts = [
    """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ehr_attachment_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 encounter_row clinic_app.ehr_encounter;
BEGIN
 IF TG_OP = 'INSERT' THEN
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = NEW.encounter_id;
   IF encounter_row.id IS NULL
      OR encounter_row.organization_id <> NEW.organization_id
      OR encounter_row.clinic_id <> NEW.clinic_id
      OR encounter_row.patient_id <> NEW.patient_id
      OR encounter_row.physician_id <> NEW.uploader_id
      OR NEW.state <> 'quarantined' OR NEW.scan_attempts <> 0
      OR NEW.scan_reason <> '' OR NEW.scanned_at IS NOT NULL
      OR NEW.declared_type <> NEW.detected_type
      OR NEW.declared_type NOT IN ('application/pdf','image/jpeg','image/png')
      OR NEW.size_bytes < 1 OR NEW.size_bytes > 10485760
      OR NEW.sha256 !~ '^[0-9a-f]{64}$'
      OR NEW.storage_key !~ '^[0-9a-f]{64}$' THEN
     RAISE EXCEPTION 'invalid attachment binding' USING ERRCODE = '23514';
   END IF;
 ELSE
   IF (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.encounter_id,NEW.patient_id,
       NEW.uploader_id,NEW.storage_key,NEW.file_name,NEW.declared_type,
       NEW.detected_type,NEW.size_bytes,NEW.sha256,NEW.created_at)
      IS DISTINCT FROM
      (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.encounter_id,OLD.patient_id,
       OLD.uploader_id,OLD.storage_key,OLD.file_name,OLD.declared_type,
       OLD.detected_type,OLD.size_bytes,OLD.sha256,OLD.created_at)
      OR OLD.state <> 'quarantined'
      OR NEW.scan_attempts <> OLD.scan_attempts + 1
      OR NOT clinic_app.ehr_assigned(NEW.encounter_id) THEN
     RAISE EXCEPTION 'invalid attachment transition' USING ERRCODE = '23514';
   END IF;
   IF NEW.state = 'quarantined' THEN
     -- Retry metadata only: a failed scan keeps quarantine with a reason.
     IF NEW.scan_reason = '' OR NEW.scanned_at IS NOT NULL THEN
       RAISE EXCEPTION 'invalid attachment retry metadata' USING ERRCODE = '23514';
     END IF;
   ELSIF NEW.scanned_at IS NULL THEN
     RAISE EXCEPTION 'invalid attachment transition' USING ERRCODE = '23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_attachment_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_attachment_guard(),
 clinic_app.questionnaire_immutable() TO clinic_owner;
RESET ROLE;
""",
    f"""
ALTER TABLE clinic_app.{TABLE} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{TABLE} FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.{TABLE} TO clinic_owner
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid)
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid);
REVOKE ALL ON clinic_app.{TABLE} FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.{TABLE} TO clinic_app;
GRANT UPDATE (state, scan_attempts, scan_reason, scanned_at)
 ON clinic_app.{TABLE} TO clinic_app;
CREATE TRIGGER ehr_attachment_binding BEFORE INSERT OR UPDATE
 ON clinic_app.{TABLE} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ehr_attachment_guard();
CREATE TRIGGER ehr_attachment_immutable BEFORE DELETE
 ON clinic_app.{TABLE} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE POLICY attachment_read ON clinic_app.{TABLE}
 FOR SELECT TO clinic_app
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND (clinic_app.ehr_assigned(encounter_id)
   OR (state = 'available' AND clinic_app.ehr_care(encounter_id))));
CREATE POLICY attachment_insert ON clinic_app.{TABLE}
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND uploader_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND clinic_app.ehr_assigned(encounter_id));
CREATE POLICY attachment_scan ON clinic_app.{TABLE}
 FOR UPDATE TO clinic_app
 USING (state = 'quarantined' AND clinic_app.ehr_assigned(encounter_id))
 WITH CHECK (state IN ('quarantined', 'available', 'rejected')
   AND clinic_app.ehr_assigned(encounter_id));
""",
    """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ehr_attachment_guard(),
 clinic_app.questionnaire_immutable() FROM clinic_owner;
RESET ROLE;
""",
]
SQL = "".join(_sql_parts)

REVERSE_SQL = f"""
DROP TRIGGER ehr_attachment_binding ON clinic_app.{TABLE};
DROP TRIGGER ehr_attachment_immutable ON clinic_app.{TABLE};
DROP POLICY attachment_read ON clinic_app.{TABLE};
DROP POLICY attachment_insert ON clinic_app.{TABLE};
DROP POLICY attachment_scan ON clinic_app.{TABLE};
DROP POLICY setup_tenant ON clinic_app.{TABLE};
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ehr_attachment_guard();
RESET ROLE;
"""
