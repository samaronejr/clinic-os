"""Audited patient downloads of released result sources (todo 50 portal)."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.orders_patient_download_scope(requested_version uuid)
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT a.session_id,a.organization_id,a.clinic_id
 FROM clinic_app.current_patient_authority('records') a
 WHERE requested_version IS NOT NULL AND EXISTS (
   SELECT 1 FROM clinic_app.orders_patient_sources(requested_version) source
   WHERE source.version_id=requested_version)
$f$;
REVOKE ALL ON FUNCTION clinic_app.orders_patient_download_scope(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.orders_patient_download_scope(uuid)
 TO clinic_owner;
RESET ROLE;
CREATE FUNCTION clinic_app.orders_patient_download_audit(record_id uuid,
 occurred_at_utc timestamptz,content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint;
BEGIN
 SELECT * INTO s FROM clinic_app.orders_patient_download_scope(record_id);
 IF s.session_id IS NULL THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 IF current_setting('transaction_isolation')<>'read committed'
    OR content_hash IS NULL OR octet_length(content_hash)<>32
    OR occurred_at_utc IS NULL
    OR occurred_at_utc NOT BETWEEN clock_timestamp()-interval '5 minutes'
      AND clock_timestamp()+interval '5 minutes' THEN
   RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
   ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event
 (organization_id,actor_user_id,event_type,component_id,component_ip,
  affected_record_type,affected_record_id,occurred_at_utc,payload,prev_hash,
  curr_hash)
 VALUES (s.organization_id,s.session_id,'orders.result.patient_downloaded',
  'clinic-os-web',NULL,'orders.result_version',record_id::text,occurred_at_utc,
  jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb','downloaded'),
  previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.orders_patient_download_audit(
 uuid,timestamptz,bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.orders_patient_download_audit(
 uuid,timestamptz,bytea) TO clinic_app;
"""

REVERSE_SQL = """
DROP FUNCTION clinic_app.orders_patient_download_audit(uuid,timestamptz,bytea);
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.orders_patient_download_scope(uuid);
RESET ROLE;
"""
