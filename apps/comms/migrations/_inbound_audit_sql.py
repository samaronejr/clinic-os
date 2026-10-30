"""Inbox ingest audit binds the authenticated provider principal, never a staff user.

Mirrors the lab pattern: a resolver proves the record belongs to the current
principal's account and tenant, then a definer function appends the hash-chained
row with the principal as actor. Payload keys are only clinic_id and object_verb.
"""

SQL = """
GRANT SELECT ON clinic_app.comms_message, clinic_app.comms_conversation,
 clinic_app.comms_templateversion TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.comms_audit_scope(record_id uuid, event_name text)
RETURNS TABLE(principal_id uuid, organization_id uuid, clinic_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT a.principal_id, a.organization_id, a.clinic_id
 FROM clinic_app.comms_provideraccount a
 WHERE a.active
   AND a.principal_id = NULLIF(current_setting('app.current_principal', true), '')::uuid
   AND a.organization_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
   AND NULLIF(current_setting('app.current_user_id', true), '') IS NULL
   AND (
     (event_name = 'comms.inbound.message_received' AND EXISTS (
        SELECT 1 FROM clinic_app.comms_message m
        JOIN clinic_app.comms_conversation c ON c.id = m.conversation_id
        WHERE m.id = record_id AND m.direction = 'in'
          AND c.provider_account_id = a.id))
     OR (event_name = 'comms.inbound.escalated' AND EXISTS (
        SELECT 1 FROM clinic_app.comms_conversation c
        WHERE c.id = record_id AND c.needs_clinician
          AND c.provider_account_id = a.id))
     OR (event_name = 'comms.inbound.status_applied' AND EXISTS (
        SELECT 1 FROM clinic_app.comms_message m
        JOIN clinic_app.comms_conversation c ON c.id = m.conversation_id
        WHERE m.id = record_id AND m.direction = 'out'
          AND c.provider_account_id = a.id))
     OR (event_name = 'comms.inbound.template_status_applied' AND EXISTS (
        SELECT 1 FROM clinic_app.comms_templateversion t
        WHERE t.id = record_id AND t.clinic_id = a.clinic_id
          AND t.channel = a.channel)))
$f$;
REVOKE ALL ON FUNCTION clinic_app.comms_audit_scope(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_audit_scope(uuid, text)
 TO clinic_app, clinic_owner;
RESET ROLE;
CREATE FUNCTION clinic_app.comms_ingest_audit(record_id uuid, event_name text,
 occurred_at_utc timestamptz, content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint;
 verb text; record_type text;
BEGIN
 SELECT * INTO s FROM clinic_app.comms_audit_scope(record_id, event_name);
 IF s.principal_id IS NULL THEN
  RAISE EXCEPTION 'inbound access denied' USING ERRCODE = '42501'; END IF;
 IF current_setting('transaction_isolation') <> 'read committed'
    OR content_hash IS NULL OR octet_length(content_hash) <> 32
    OR occurred_at_utc IS NULL OR occurred_at_utc NOT BETWEEN
       clock_timestamp() - interval '5 minutes'
       AND clock_timestamp() + interval '5 minutes'
 THEN RAISE EXCEPTION 'invalid audit input' USING ERRCODE = '22023'; END IF;
 verb := CASE event_name
  WHEN 'comms.inbound.message_received' THEN 'received'
  WHEN 'comms.inbound.escalated' THEN 'escalated' ELSE 'status_applied' END;
 record_type := CASE event_name
  WHEN 'comms.inbound.escalated' THEN 'comms.conversation'
  WHEN 'comms.inbound.template_status_applied' THEN 'comms.template_version'
  ELSE 'comms.message' END;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  ('clinic-audit:' || s.organization_id::text) COLLATE pg_catalog."C", 0));
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id = s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash := coalesce(previous_hash, decode(repeat('00', 32), 'hex'));
 INSERT INTO clinic_app.audit_event
 (organization_id, actor_user_id, event_type, component_id, component_ip,
  affected_record_type, affected_record_id, occurred_at_utc, payload,
  prev_hash, curr_hash)
 VALUES (s.organization_id, s.principal_id, event_name, 'clinic-os-web', NULL,
  record_type, record_id::text, occurred_at_utc,
  jsonb_build_object('clinic_id', s.clinic_id::text, 'object_verb', verb),
  previous_hash, clinic_app.digest(content_hash || previous_hash, 'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_ingest_audit(uuid, text, timestamptz, bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_ingest_audit(uuid, text, timestamptz, bytea)
 TO clinic_app;
"""

REVERSE_SQL = """
DROP FUNCTION IF EXISTS
 clinic_app.comms_ingest_audit(uuid, text, timestamptz, bytea);
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION IF EXISTS clinic_app.comms_audit_scope(uuid, text);
RESET ROLE;
REVOKE SELECT ON clinic_app.comms_message, clinic_app.comms_conversation,
 clinic_app.comms_templateversion FROM clinic_resolver;
"""
