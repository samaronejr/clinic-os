"""Append the patient's audio-mode transition audit under patient authority.

A patient session carries no tenant or user GUC, so ``record_phase1_event``
cannot append for it. The resolver-owned scope admits only the bound patient
session whose newest own mode event matches the claimed transition; the
owner-owned writer then appends one tenant-chain row with the fixed payload and
refuses a second append for the same transition.
"""

_sql = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.teleconsult_mode_audit_scope(
 requested_session uuid, event_name text)
RETURNS TABLE(patient_session uuid, organization_id uuid, clinic_id uuid,
 object_verb text, changed_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT NULLIF(current_setting('app.current_patient_session', true), '')::uuid,
 s.organization_id, s.clinic_id, e.kind, e.created_at
 FROM clinic_app.teleconsult_teleconsultsession s
 CROSS JOIN LATERAL (
  SELECT ev.kind::text AS kind, ev.created_at
  FROM clinic_app.teleconsult_teleconsultevent ev
  WHERE ev.session_id=s.id AND ev.actor_role='patient'
  AND ev.kind IN ('audio_only','video_restored')
  ORDER BY ev.created_at DESC, ev.id DESC LIMIT 1) e
 WHERE s.id=requested_session
 AND clinic_app.teleconsult_patient_match(s.id)
 AND e.kind = CASE event_name
  WHEN 'teleconsult.audio_only.enabled' THEN 'audio_only'
  WHEN 'teleconsult.audio_only.disabled' THEN 'video_restored' END
$f$;
REVOKE ALL ON FUNCTION clinic_app.teleconsult_mode_audit_scope(uuid,text)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.teleconsult_mode_audit_scope(uuid,text)
 TO clinic_owner;
RESET ROLE;
CREATE FUNCTION clinic_app.teleconsult_mode_audit(requested_session uuid,
 event_name text, occurred_at_utc timestamptz, content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint;
BEGIN
 SELECT * INTO s
 FROM clinic_app.teleconsult_mode_audit_scope(requested_session,event_name);
 IF s.patient_session IS NULL THEN
 RAISE EXCEPTION 'teleconsult participant authority required'
 USING ERRCODE='42501'; END IF;
 IF current_setting('transaction_isolation') <> 'read committed'
 OR content_hash IS NULL OR octet_length(content_hash) <> 32
 OR occurred_at_utc IS NULL OR occurred_at_utc < s.changed_at
 OR occurred_at_utc NOT BETWEEN clock_timestamp()-interval '5 minutes'
 AND clock_timestamp()+interval '5 minutes' THEN
 RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
 ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 IF EXISTS (SELECT 1 FROM clinic_app.audit_event a
  WHERE a.organization_id=s.organization_id AND a.event_type=event_name
  AND a.affected_record_id=requested_session::text
  AND a.actor_user_id=s.patient_session
  AND a.occurred_at_utc>=s.changed_at) THEN
 RAISE EXCEPTION 'teleconsult mode transition already audited'
 USING ERRCODE='42501'; END IF;
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event (organization_id,actor_user_id,event_type,
 component_id,component_ip,affected_record_type,affected_record_id,
 occurred_at_utc,payload,prev_hash,curr_hash)
 VALUES (s.organization_id,s.patient_session,event_name,'clinic-os-web',NULL,
 'teleconsult.session',requested_session::text,occurred_at_utc,
 jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb',s.object_verb),
 previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END
$f$;
REVOKE ALL ON FUNCTION
 clinic_app.teleconsult_mode_audit(uuid,text,timestamptz,bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
 clinic_app.teleconsult_mode_audit(uuid,text,timestamptz,bytea) TO clinic_app;
"""

SQL = _sql
REVERSE_SQL = """
DROP FUNCTION clinic_app.teleconsult_mode_audit(uuid,text,timestamptz,bytea);
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.teleconsult_mode_audit_scope(uuid,text);
RESET ROLE;
"""
