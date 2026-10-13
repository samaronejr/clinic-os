"""One immutable mode transition owns one canonical audit receipt.

The receipt is stamped by the INSERT trigger, never supplied by a caller.
Both participant channels use the same writer; no event can commit first and
wait for a later audit call. Callers cannot append or replace a receipt.
"""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.teleconsult_mode_transition_scope(
 requested_session uuid, requested_role text)
RETURNS TABLE(actor_id uuid, organization_id uuid, clinic_id uuid,
 latest_kind text)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT CASE requested_role
  WHEN 'patient' THEN
   NULLIF(current_setting('app.current_patient_session',true),'')::uuid
  WHEN 'physician' THEN
   NULLIF(current_setting('app.current_user_id',true),'')::uuid END,
 s.organization_id, s.clinic_id, e.kind
 FROM clinic_app.teleconsult_teleconsultsession s
 LEFT JOIN LATERAL (
  SELECT ev.kind::text AS kind
  FROM clinic_app.teleconsult_teleconsultevent ev
  WHERE ev.session_id=s.id AND ev.actor_role=requested_role
  AND ev.kind IN ('audio_only','video_restored')
  ORDER BY ev.mode_audit_seq DESC
  LIMIT 1) e ON true
 WHERE s.id=requested_session
 AND s.organization_id<>'00000000-0000-0000-0000-000000000000'::uuid
 AND ((requested_role='physician' AND clinic_app.teleconsult_clinician(s.id))
  OR (requested_role='patient' AND clinic_app.teleconsult_patient_match(s.id)))
$f$;
REVOKE ALL ON FUNCTION
 clinic_app.teleconsult_mode_transition_scope(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
 clinic_app.teleconsult_mode_transition_scope(uuid,text) TO clinic_owner;
RESET ROLE;

CREATE FUNCTION clinic_app.teleconsult_mode_transition_audit()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE s RECORD; event_name text; content text; content_hash bytea;
 previous_hash bytea;
BEGIN
 IF NEW.mode_audit_seq IS NOT NULL THEN
 RAISE EXCEPTION 'teleconsult audit receipt cannot be supplied'
 USING ERRCODE='42501'; END IF;
 IF NEW.kind NOT IN ('audio_only','video_restored') THEN RETURN NEW; END IF;
 SELECT * INTO s FROM clinic_app.teleconsult_mode_transition_scope(
  NEW.session_id,NEW.actor_role);
 IF s.actor_id IS NULL THEN
 RAISE EXCEPTION 'teleconsult participant authority required'
 USING ERRCODE='42501'; END IF;
 IF current_setting('transaction_isolation') <> 'read committed' THEN
 RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 PERFORM pg_advisory_xact_lock(hashtextextended(
  'teleconsult.session:'||NEW.session_id::text,0));
 -- A VOLATILE scope reads the committed state/authority after lock admission,
 -- not the INSERT statement's pre-wait snapshot.
 SELECT * INTO s FROM clinic_app.teleconsult_mode_transition_scope(
  NEW.session_id,NEW.actor_role);
 IF s.actor_id IS NULL THEN
 RAISE EXCEPTION 'teleconsult participant authority required'
 USING ERRCODE='42501'; END IF;
 IF s.latest_kind=NEW.kind
 OR (s.latest_kind IS NULL AND NEW.kind='video_restored') THEN
 RETURN NULL; END IF;
 NEW.created_at:=statement_timestamp();
 event_name:=CASE NEW.kind WHEN 'audio_only' THEN
  'teleconsult.audio_only.enabled' ELSE 'teleconsult.audio_only.disabled' END;
 -- RFC 8785 for this closed registered-event subset: UUIDs and fixed ASCII
 -- vocabulary only. No caller text/JSON/digest is accepted here.
 content:=format(
  '{"actor":{"user_id":"%s"},"affected_record_id":"%s",'
  '"affected_record_type":"teleconsult.session",'
  '"component":{"identifier":"clinic-os-web","ip":null},"event_type":"%s",'
  '"occurred_at_utc":"%s","organization_id":"%s",'
  '"payload":{"clinic_id":"%s","object_verb":"%s"},"v":"clinic-audit-v1"}',
  s.actor_id::text,NEW.session_id::text,event_name,
  to_char(NEW.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  s.organization_id::text,s.clinic_id::text,NEW.kind);
 content_hash:=clinic_app.digest(convert_to('clinic-audit-v1','UTF8')
  ||decode('00','hex')||convert_to(content,'UTF8'),'sha256');
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event (organization_id,actor_user_id,event_type,
  component_id,component_ip,affected_record_type,affected_record_id,
  occurred_at_utc,payload,prev_hash,curr_hash)
 VALUES (s.organization_id,s.actor_id,event_name,'clinic-os-web',NULL,
  'teleconsult.session',NEW.session_id::text,NEW.created_at,
  jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb',NEW.kind),
  previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO NEW.mode_audit_seq;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.teleconsult_mode_transition_audit()
 FROM PUBLIC,clinic_app;
CREATE TRIGGER teleconsult_mode_audit BEFORE INSERT
 ON clinic_app.teleconsult_teleconsultevent FOR EACH ROW
 EXECUTE FUNCTION clinic_app.teleconsult_mode_transition_audit();
"""

REVERSE_SQL = """
DROP TRIGGER teleconsult_mode_audit
 ON clinic_app.teleconsult_teleconsultevent;
DROP FUNCTION clinic_app.teleconsult_mode_transition_audit();
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.teleconsult_mode_transition_scope(uuid,text);
RESET ROLE;
"""
