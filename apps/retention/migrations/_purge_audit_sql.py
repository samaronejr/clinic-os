"""Use the canonical chain with the executing retention principal, never a user."""

SQL = r"""

SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.retention_audit_scope(record_id uuid,event_name text)
RETURNS TABLE(organization_id uuid,clinic_id uuid,principal_id uuid,record_type text)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT p.organization_id,p.clinic_id,
 NULLIF(current_setting('app.current_principal',true),'')::uuid,
 'retention.artifact_purge'::text FROM clinic_app.retention_artifactpurge p
 WHERE p.id=$1 AND p.organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.retention_authorized(p.clinic_id)
 AND (event_name='retention.purge.'||p.state
  OR (event_name='retention.purge.failure' AND p.failure_code<>''))
$f$;
REVOKE ALL ON FUNCTION clinic_app.retention_audit_scope(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_audit_scope(uuid,text)
 TO clinic_agent,clinic_owner;
RESET ROLE;

CREATE FUNCTION clinic_app.retention_audit(record_id uuid,event_name text,
 occurred_at_utc timestamptz,content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint; verb text; record_type text;
BEGIN
 SELECT * INTO s FROM clinic_app.retention_audit_scope(record_id,event_name);
 IF s.principal_id IS NULL THEN
  RAISE EXCEPTION 'retention audit denied' USING ERRCODE='42501'; END IF;
 IF current_setting('transaction_isolation')<>'read committed'
 OR content_hash IS NULL OR octet_length(content_hash)<>32
 OR occurred_at_utc IS NULL OR occurred_at_utc NOT BETWEEN
 clock_timestamp()-interval '5 minutes' AND clock_timestamp()+interval '5 minutes'
 THEN RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023'; END IF;
 verb:=split_part(event_name,'.',3); record_type:=s.record_type;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event
 (organization_id,actor_user_id,event_type,component_id,component_ip,
 affected_record_type,affected_record_id,occurred_at_utc,payload,prev_hash,curr_hash)
 VALUES(s.organization_id,s.principal_id,event_name,'clinic-os-worker',NULL,
 record_type,record_id::text,occurred_at_utc,
 jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb',verb),
 previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_audit(uuid,text,timestamptz,bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_audit(uuid,text,timestamptz,bytea)
 TO clinic_agent;

"""
