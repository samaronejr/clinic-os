"""Confirmation is the sole bridge from untrusted ciphertext to an observation."""

from apps.orders.migrations._access_sql import render

SQL = render(r"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_review_extraction(
 requested uuid,target uuid,decision text,source bytea,kek text)
RETURNS TABLE(observation_id uuid,created boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE c clinic_app.ai_extractioncandidate; j clinic_app.ai_extractionjob;
 v clinic_app.orders_resultversion; p jsonb; expected jsonb; parsed text;
 identifier uuid; actor uuid:=__ACTOR__;
BEGIN
 SELECT * INTO c FROM clinic_app.ai_extractioncandidate WHERE id=requested;
 SELECT * INTO j FROM clinic_app.ai_extractionjob WHERE id=c.job_id;
 IF c.id IS NULL OR c.organization_id IS DISTINCT FROM __TENANT__
 OR NOT clinic_app.ai_extraction_job_allowed(j.id)
 OR NOT clinic_app.has_permission('clinical.write',j.clinic_id,j.patient_enrollment_id)
 OR decision IS NULL OR decision NOT IN ('confirmed','rejected') THEN
  RAISE EXCEPTION 'extraction denied' USING ERRCODE='42501'; END IF;
 IF decision='confirmed' THEN
  SELECT * INTO v FROM clinic_app.orders_resultversion WHERE id=target;
  IF v.id IS NULL OR (v.organization_id,v.clinic_id,v.sha256) IS DISTINCT FROM
    (j.organization_id,j.clinic_id,j.sha256)
  OR (j.result_version_id IS NOT NULL AND j.result_version_id<>v.id) THEN
   RAISE EXCEPTION 'extraction denied' USING ERRCODE='42501'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('orders-series:'||v.result_id,0));
 END IF;
 SELECT * INTO c FROM clinic_app.ai_extractioncandidate WHERE id=requested FOR UPDATE;
 IF c.status=decision AND c.reviewed_by_id=actor AND
   (decision='rejected' OR EXISTS(SELECT 1 FROM clinic_app.orders_resultobservation o
     WHERE o.id=c.observation_id AND o.version_id=target))
 THEN RETURN QUERY SELECT c.observation_id,false; RETURN; END IF;
 IF c.status NOT IN ('candidate','ambiguous') OR c.expires_at<=statement_timestamp()
 THEN RAISE EXCEPTION 'extraction decision stale' USING ERRCODE='40001'; END IF;
 IF decision='rejected' THEN
  UPDATE clinic_app.ai_extractioncandidate SET status='rejected',
   reviewed_by_id=actor,reviewed_at=statement_timestamp() WHERE id=c.id;
  RETURN QUERY SELECT NULL::uuid,true; RETURN;
 END IF;
 IF c.status<>'candidate' OR NOT clinic_app.orders_is_current(v.id)
 OR NOT EXISTS(SELECT 1 FROM clinic_app.orders_resultmatch m
  JOIN clinic_app.orders_order o ON o.id=m.order_id
   AND o.organization_id=j.organization_id AND o.clinic_id=j.clinic_id
  WHERE m.version_id=v.id AND m.organization_id=j.organization_id
  AND m.clinic_id=j.clinic_id AND o.enrollment_id=j.patient_enrollment_id)
 OR EXISTS(SELECT 1 FROM clinic_app.orders_resultacknowledgment a
  WHERE a.version_id=v.id)
 THEN RAISE EXCEPTION 'extraction filing denied' USING ERRCODE='23514'; END IF;
 p:=convert_from(clinic_app.tenant_decrypt(
  kek,'ai.extraction.candidate.v1',c.payload),'UTF8')::jsonb;
 IF jsonb_typeof(p) IS DISTINCT FROM 'object' THEN
  RAISE EXCEPTION 'extraction value unresolved' USING ERRCODE='23514'; END IF;
 IF NOT p ?& ARRAY['field','kind','raw_text','value','parsed_value','unit',
  'reference_interval','region','status','reason']
 OR p-ARRAY['field','kind','raw_text','value','parsed_value','unit',
  'reference_interval','region','status','reason']<>'{}'::jsonb
 OR EXISTS(SELECT 1 FROM jsonb_each(p) x WHERE x.key<>'region'
  AND jsonb_typeof(x.value)<>'string')
 OR jsonb_typeof(p->'region') IS DISTINCT FROM 'object' THEN
  RAISE EXCEPTION 'extraction value unresolved' USING ERRCODE='23514'; END IF;
 IF NOT (p->'region') ?& ARRAY['page','bbox']
 OR (p->'region')-ARRAY['page','bbox']<>'{}'::jsonb
 OR jsonb_typeof(p->'region'->'page') IS DISTINCT FROM 'number'
 OR p->'region'->>'page'!~'^[1-9][0-9]?$'
 OR (p->'region'->>'page')::int>jsonb_array_length(j.rotations)
 OR jsonb_typeof(p->'region'->'bbox') IS DISTINCT FROM 'array' THEN
  RAISE EXCEPTION 'extraction region unresolved' USING ERRCODE='23514'; END IF;
 IF jsonb_array_length(p->'region'->'bbox')<>4
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(p->'region'->'bbox') x
  WHERE jsonb_typeof(x)<>'number' OR x::text!~'^[0-9]{1,5}$'
  OR x::text::int>10000)
 OR (p->'region'->'bbox'->>0)::int>=(p->'region'->'bbox'->>2)::int
 OR (p->'region'->'bbox'->>1)::int>=(p->'region'->'bbox'->>3)::int THEN
  RAISE EXCEPTION 'extraction region unresolved' USING ERRCODE='23514'; END IF;
 IF p->>'status'<>'candidate' OR p->>'reason'<>''
 OR p->>'field'!~'^[a-z][a-z0-9_]{0,31}$'
 OR length(p->>'raw_text')>1024 OR length(p->>'value') NOT BETWEEN 1 AND 128
 OR length(p->>'reference_interval')>256
 OR position(p->>'value' IN p->>'raw_text')=0
 OR (p->>'reference_interval'<>'' AND
  position(p->>'reference_interval' IN p->>'raw_text')=0)
 THEN RAISE EXCEPTION 'extraction value unresolved' USING ERRCODE='23514'; END IF;
 IF p->>'kind'='decimal' THEN
  IF p->>'value'!~('^[+-]?((0|[1-9][0-9]*)(,[0-9]+)?|'
   '[1-9][0-9]{0,2}(\.[0-9]{3})+,[0-9]+)$')
  OR p->>'unit'<>ALL(ARRAY['mg/dL','g/dL','mmol/L','mEq/L','U/L','ng/mL','pg/mL',
   'mcg/L',U&'\00B5g/L',U&'10^3/\00B5L',U&'10^6/\00B5L',
   '%','mL','L','kg','g','cm','m',
   'mmHg','bpm','kg/m2','1'])
  OR position(p->>'unit' IN p->>'raw_text')=0 THEN
   RAISE EXCEPTION 'extraction value unresolved' USING ERRCODE='23514'; END IF;
  parsed:=replace(replace(p->>'value','.',''),',','.');
 ELSIF p->>'kind'='date' AND p->>'unit'='' AND
  p->>'value'~'^[0-9]{2}/[0-9]{2}/[0-9]{4}$' THEN
  BEGIN
   parsed:=to_char(make_date(substring(p->>'value',7,4)::int,
    substring(p->>'value',4,2)::int,substring(p->>'value',1,2)::int),'YYYY-MM-DD');
  EXCEPTION WHEN datetime_field_overflow THEN
   RAISE EXCEPTION 'extraction value unresolved' USING ERRCODE='23514';
  END;
 ELSE RAISE EXCEPTION 'extraction value unresolved' USING ERRCODE='23514'; END IF;
 IF parsed IS DISTINCT FROM p->>'parsed_value' THEN
  RAISE EXCEPTION 'extraction value unresolved' USING ERRCODE='23514'; END IF;
 expected:=jsonb_build_object('code',p->>'field','value',p->>'value',
  'parsed_value',parsed,'unit',p->>'unit','reference_interval',p->>'reference_interval',
  'abnormal_flag','','provenance','imported','confirmer',actor::text,
  'page',p->'region'->'page','bbox',p->'region'->'bbox','raw_text',p->>'raw_text',
  'extraction_candidate',c.id::text);
 IF convert_from(clinic_app.tenant_decrypt(
  kek,'orders.observation.source',source),'UTF8')::jsonb
  IS DISTINCT FROM expected THEN
  RAISE EXCEPTION 'extraction payload mismatch' USING ERRCODE='23514'; END IF;
 identifier:=gen_random_uuid();
 -- The FK is deferred. The uncommitted marker admits only this exact row,
 -- created in this function, and cannot be forged through runtime table ACLs.
 UPDATE clinic_app.ai_extractioncandidate SET status='confirmed',
  reviewed_by_id=actor,reviewed_at=statement_timestamp(),observation_id=identifier
  WHERE id=c.id;
 INSERT INTO clinic_app.orders_resultobservation
 (id,organization_id,clinic_id,created_at,version_id,position,source)
 VALUES(identifier,j.organization_id,j.clinic_id,statement_timestamp(),v.id,
  (SELECT count(*) FROM clinic_app.orders_resultobservation o
   WHERE o.version_id=v.id),source);
 RETURN QUERY SELECT identifier,true;
EXCEPTION WHEN data_exception THEN
 RAISE EXCEPTION 'extraction data invalid' USING ERRCODE='23514';
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_review_extraction(uuid,uuid,text,bytea,text)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_review_extraction(uuid,uuid,text,bytea,text)
 TO clinic_app;
RESET ROLE;
""")
