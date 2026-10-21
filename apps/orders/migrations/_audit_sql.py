"""Lab-origin audit rows bind the actual principal, never an ordering physician."""

FUNCTIONS = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.orders_audit_scope(record_id uuid,event_name text)
RETURNS TABLE(principal_id uuid,organization_id uuid,clinic_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT a.principal_id,a.organization_id,a.clinic_id
 FROM clinic_app.orders_labaccount a WHERE
 a.id=clinic_app.orders_lab_account(a.clinic_id) AND (
 event_name IN ('orders.result.ingested','orders.result.matched',
  'orders.result.quarantined') AND EXISTS(
 SELECT 1 FROM clinic_app.orders_resultversion v
 WHERE v.id=record_id AND v.lab_account_id=a.id)
 OR event_name='orders.task.created' AND EXISTS(
 SELECT 1 FROM clinic_app.orders_resultworkitem w
 JOIN clinic_app.orders_resultversion v ON v.id=w.version_id
 WHERE w.task_id=record_id AND v.lab_account_id=a.id))
$f$;
REVOKE ALL ON FUNCTION clinic_app.orders_audit_scope(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.orders_audit_scope(uuid,text)
 TO clinic_app,clinic_owner;
RESET ROLE;
CREATE FUNCTION clinic_app.orders_lab_audit(record_id uuid,event_name text,
 occurred_at_utc timestamptz,content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint; verb text; record_type text;
BEGIN
 SELECT * INTO s FROM clinic_app.orders_audit_scope(record_id,event_name);
 IF s.principal_id IS NULL THEN
  RAISE EXCEPTION 'result access denied' USING ERRCODE='42501'; END IF;
 IF current_setting('transaction_isolation')<>'read committed'
 OR content_hash IS NULL OR octet_length(content_hash)<>32
 OR occurred_at_utc IS NULL OR occurred_at_utc NOT BETWEEN
 clock_timestamp()-interval '5 minutes' AND clock_timestamp()+interval '5 minutes'
 THEN RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023'; END IF;
 verb:=substring(event_name from length('orders.result.')+1);
 record_type:='orders.result_version';
 IF event_name='orders.task.created' THEN verb:='created';
  record_type:='workflows.task'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event
 (organization_id,actor_user_id,event_type,component_id,component_ip,
 affected_record_type,affected_record_id,occurred_at_utc,payload,prev_hash,curr_hash)
 VALUES(s.organization_id,s.principal_id,event_name,'clinic-os-web',NULL,
 record_type,record_id::text,occurred_at_utc,
 jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb',verb),
 previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.orders_lab_audit(uuid,text,timestamptz,bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.orders_lab_audit(uuid,text,timestamptz,bytea)
 TO clinic_app;
"""
