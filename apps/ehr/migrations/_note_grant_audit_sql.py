"""One registered metadata-only audit receipt owns each grant revocation edge."""

from typing import Final

# As for the scheduling and teleconsult SQL writers, every canonical value is a
# UUID, a fixed ASCII string or a UTC microsecond timestamp, never caller text.
SQL: Final = """
CREATE FUNCTION clinic_app.ehr_note_revocation_audit()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $f$
DECLARE
 actor_id uuid := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 occurred_at_utc timestamptz;
 payload text := '{"clinic_id":"' || NEW.clinic_id::text
  || '","object_verb":"revoked"}';
 content text; content_hash bytea; previous_hash bytea;
BEGIN
 IF actor_id IS NULL THEN
   RAISE EXCEPTION 'note grant audit context required' USING ERRCODE='42501';
 END IF;
 IF current_setting('transaction_isolation')<>'read committed' THEN
   RAISE EXCEPTION 'audit append requires read committed isolation'
    USING ERRCODE='22023';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  ('clinic-audit:' || NEW.organization_id::text) COLLATE pg_catalog."C",0));
 occurred_at_utc := clock_timestamp();
 content := '{"actor":{"user_id":"' || actor_id::text
  || '"},"affected_record_id":"' || NEW.id::text
  || '","affected_record_type":"ehr.note_access_grant",'
  || '"component":{"identifier":"clinic-os-web","ip":null},'
  || '"event_type":"ehr.note.revoked","occurred_at_utc":"'
  || to_char(occurred_at_utc AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
  || '","organization_id":"' || NEW.organization_id::text
  || '","payload":' || payload || ',"v":"clinic-audit-v1"}';
 content_hash := sha256(
  decode('636c696e69632d61756469742d763100','hex')
  || convert_to(content,'UTF8'));
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
  WHERE a.organization_id=NEW.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash := coalesce(previous_hash,decode(
  '0000000000000000000000000000000000000000000000000000000000000000','hex'));
 INSERT INTO clinic_app.audit_event (
  organization_id,actor_user_id,event_type,component_id,component_ip,
  affected_record_type,affected_record_id,occurred_at_utc,payload,
  prev_hash,curr_hash)
 VALUES (
  NEW.organization_id,actor_id,'ehr.note.revoked','clinic-os-web',NULL,
  'ehr.note_access_grant',NEW.id::text,occurred_at_utc,payload::jsonb,
  previous_hash,sha256(content_hash || previous_hash));
 RETURN NULL;
END $f$;
ALTER FUNCTION clinic_app.ehr_note_revocation_audit() OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.ehr_note_revocation_audit()
 FROM PUBLIC,clinic_app,clinic_resolver;
CREATE TRIGGER ehr_note_revocation_audit
 AFTER UPDATE OF revoked_at ON clinic_app.ehr_noteaccessgrant
 FOR EACH ROW WHEN (OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL)
 EXECUTE FUNCTION clinic_app.ehr_note_revocation_audit();
"""
