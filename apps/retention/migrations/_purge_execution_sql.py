"""Retention commands expose metadata only; no machine can read clinical content."""

SQL = r"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.retention_schedule(clinic uuid,record_class text)
RETURNS TABLE(dry_run boolean,due bigint,blocked bigint,principal_id uuid,
 purge_id uuid,event_state text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE candidate record; p clinic_app.retention_artifactpurge;
 org uuid; wanted text; existing boolean;
BEGIN
 IF NOT clinic_app.retention_authorized(clinic)
 OR record_class NOT IN ('raw_audio','ai_artifact','transcript_partial')
 THEN RETURN; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('clinic-configuration:'||clinic,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('retention:'||clinic,0));
 principal_id:=NULLIF(current_setting('app.current_principal',true),'')::uuid;
 org:=NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 SELECT c.retention_dry_run INTO dry_run
 FROM clinic_app.identity_clinicconfiguration c WHERE c.clinic_id=clinic
 ORDER BY c.version DESC LIMIT 1;
 dry_run:=coalesce(dry_run,true); due:=0; blocked:=0;
 FOR candidate IN
  WITH due_records AS (
  SELECT ids.id,clinic_app.retention_due(clinic,record_class,ids.id) AS expires,
   clinic_app.retention_held(clinic,record_class,ids.id) AS held
  FROM (
   SELECT s.id FROM clinic_app.scribe_recordingsession s
   WHERE record_class='raw_audio' AND s.clinic_id=clinic AND s.organization_id=org
   UNION ALL
   SELECT a.id FROM clinic_app.ai_aiartifact a
   WHERE a.clinic_id=clinic AND a.organization_id=org
    AND ((record_class='transcript_partial'
     AND a.retention_class='transcript_partial')
    OR (record_class='ai_artifact'
     AND a.retention_class IN ('transient_input','review_draft')))
  ) ids
  WHERE clinic_app.retention_due(clinic,record_class,ids.id)<=statement_timestamp()
  AND NOT EXISTS(SELECT 1 FROM clinic_app.retention_artifactpurge prior
   WHERE prior.clinic_id=clinic AND prior.record_class=retention_schedule.record_class
   AND prior.record_id=ids.id AND prior.state='purged')
  ), runnable AS (
   SELECT * FROM due_records WHERE NOT held ORDER BY expires,id LIMIT 100
  ), held_records AS (
   SELECT * FROM due_records WHERE held ORDER BY expires,id LIMIT 100
  )
  SELECT * FROM runnable UNION ALL SELECT * FROM held_records
  ORDER BY held,expires,id
 LOOP
  due:=due+1;
  IF candidate.held THEN
   blocked:=blocked+1;
  END IF;
 END LOOP;
 IF dry_run THEN
  purge_id:=NULL; event_state:=''; RETURN NEXT; RETURN;
 END IF;
 FOR candidate IN
  WITH due_records AS (
  SELECT ids.id,clinic_app.retention_due(clinic,record_class,ids.id) AS expires,
   clinic_app.retention_held(clinic,record_class,ids.id) AS held
  FROM (
   SELECT s.id FROM clinic_app.scribe_recordingsession s
   WHERE record_class='raw_audio' AND s.clinic_id=clinic AND s.organization_id=org
   UNION ALL
   SELECT a.id FROM clinic_app.ai_aiartifact a
   WHERE a.clinic_id=clinic AND a.organization_id=org
    AND ((record_class='transcript_partial'
     AND a.retention_class='transcript_partial')
    OR (record_class='ai_artifact'
     AND a.retention_class IN ('transient_input','review_draft')))
  ) ids
  WHERE clinic_app.retention_due(clinic,record_class,ids.id)<=statement_timestamp()
  AND NOT EXISTS(SELECT 1 FROM clinic_app.retention_artifactpurge prior
   WHERE prior.clinic_id=clinic AND prior.record_class=retention_schedule.record_class
   AND prior.record_id=ids.id AND prior.state='purged')
  ), runnable AS (
   SELECT * FROM due_records WHERE NOT held ORDER BY expires,id LIMIT 100
  ), held_records AS (
   SELECT * FROM due_records WHERE held ORDER BY expires,id LIMIT 100
  )
  SELECT * FROM runnable UNION ALL SELECT * FROM held_records
  ORDER BY held,expires,id
 LOOP
  wanted:=CASE WHEN candidate.held
   THEN 'blocked_hold' ELSE 'purge_due' END;
  SELECT * INTO p FROM clinic_app.retention_artifactpurge prior
  WHERE prior.clinic_id=clinic AND prior.record_class=retention_schedule.record_class
   AND prior.record_id=candidate.id FOR UPDATE;
  existing:=FOUND; event_state:='';
  IF NOT existing THEN
   INSERT INTO clinic_app.retention_artifactpurge
    (id,organization_id,clinic_id,principal_id,record_class,record_id,state,due_at,
     target_count,deleted_keys,failure_code,created_at)
   VALUES(gen_random_uuid(),org,clinic,principal_id,record_class,candidate.id,wanted,
    candidate.expires,0,'[]','',statement_timestamp()) RETURNING * INTO p;
   event_state:=wanted;
  ELSIF p.state IN ('purge_due','blocked_hold') AND p.state<>wanted THEN
   UPDATE clinic_app.retention_artifactpurge SET state=wanted
   WHERE id=p.id RETURNING * INTO p;
   event_state:=wanted;
  END IF;
  purge_id:=p.id;
  RETURN NEXT;
 END LOOP;
 IF due=0 THEN purge_id:=NULL; event_state:=''; RETURN NEXT; END IF;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_schedule(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_schedule(uuid,text) TO clinic_agent;

CREATE FUNCTION clinic_app.retention_prepare(clinic uuid,requested uuid)
RETURNS TABLE(object_keys text[],backup_expires timestamptz,event_state text,
 storage_locations text[])
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE p clinic_app.retention_artifactpurge; dry boolean; backup_days integer;
BEGIN
 IF NOT clinic_app.retention_authorized(clinic) THEN RETURN; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('clinic-configuration:'||clinic,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('retention:'||clinic,0));
 SELECT * INTO p FROM clinic_app.retention_artifactpurge r WHERE r.id=requested
 AND r.clinic_id=clinic AND r.organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid FOR UPDATE;
 IF p.id IS NULL OR p.state='purged' THEN RETURN; END IF;
 SELECT c.retention_dry_run,c.retention_backup_days INTO dry,backup_days
 FROM clinic_app.identity_clinicconfiguration c WHERE c.clinic_id=clinic
 ORDER BY c.version DESC LIMIT 1;
 IF coalesce(dry,true) THEN RETURN; END IF;
 IF clinic_app.retention_held(clinic,p.record_class,p.record_id) THEN
  UPDATE clinic_app.retention_artifactpurge SET state='blocked_hold' WHERE id=p.id;
  object_keys:=ARRAY[]::text[]; backup_expires:=p.backup_copy_expires_at;
  event_state:='blocked_hold'; RETURN NEXT; RETURN;
 END IF;
 IF p.started_at IS NULL AND (
  clinic_app.retention_due(clinic,p.record_class,p.record_id) IS NULL
  OR clinic_app.retention_due(clinic,p.record_class,p.record_id)>statement_timestamp()
 ) THEN RETURN; END IF;
 SELECT coalesce(array_agg(c.storage_key ORDER BY c.storage_key),ARRAY[]::text[])
 INTO object_keys FROM clinic_app.scribe_audiochunk c
 WHERE p.record_class='raw_audio' AND c.session_id=p.record_id;
 SELECT coalesce(array_agg(DISTINCT c.storage_location),ARRAY[]::text[])
 INTO storage_locations FROM clinic_app.scribe_audiochunk c
 WHERE p.record_class='raw_audio' AND c.session_id=p.record_id;
 backup_expires:=coalesce(p.backup_copy_expires_at,
  statement_timestamp()+coalesce(backup_days,35)*interval '1 day');
 UPDATE clinic_app.retention_artifactpurge SET state='purging',
  started_at=coalesce(started_at,statement_timestamp()),
  backup_copy_expires_at=backup_expires,target_count=cardinality(object_keys)
 WHERE id=p.id;
 PERFORM clinic_app.retention_shred(p.id);
 event_state:='purging'; RETURN NEXT;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_prepare(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_prepare(uuid,uuid) TO clinic_agent;

CREATE FUNCTION clinic_app.retention_confirm(clinic uuid,requested uuid,object_key text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE p clinic_app.retention_artifactpurge;
BEGIN
 IF NOT clinic_app.retention_authorized(clinic) THEN
  RAISE EXCEPTION 'retention denied' USING ERRCODE='42501'; END IF;
 SELECT * INTO p FROM clinic_app.retention_artifactpurge r
 WHERE r.id=requested AND r.clinic_id=clinic FOR UPDATE;
 IF p.id IS NULL OR p.state<>'purging'
 OR clinic_app.retention_held(clinic,p.record_class,p.record_id)
 OR NOT EXISTS(SELECT 1 FROM clinic_app.scribe_audiochunk c
  WHERE p.record_class='raw_audio' AND c.session_id=p.record_id
  AND c.storage_key=object_key AND c.state='purging'
  AND octet_length(c.wrapped_key)=0 AND octet_length(c.encrypted_data)=0) THEN
  RAISE EXCEPTION 'retention target denied' USING ERRCODE='23514'; END IF;
 IF NOT p.deleted_keys ? object_key THEN
  UPDATE clinic_app.retention_artifactpurge
  SET deleted_keys=deleted_keys||jsonb_build_array(object_key) WHERE id=p.id;
 END IF;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_confirm(uuid,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_confirm(uuid,uuid,text) TO clinic_agent;

CREATE FUNCTION clinic_app.retention_complete(clinic uuid,requested uuid)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE p clinic_app.retention_artifactpurge;
BEGIN
 IF NOT clinic_app.retention_authorized(clinic) THEN
  RAISE EXCEPTION 'retention denied' USING ERRCODE='42501'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('retention:'||clinic,0));
 SELECT * INTO p FROM clinic_app.retention_artifactpurge r WHERE r.id=requested
 AND r.clinic_id=clinic FOR UPDATE;
 IF p.id IS NULL OR p.state<>'purging'
 OR clinic_app.retention_held(clinic,p.record_class,p.record_id)
 OR jsonb_array_length(p.deleted_keys)<>p.target_count
 OR EXISTS(SELECT 1 FROM clinic_app.scribe_audiochunk c
  WHERE p.record_class='raw_audio' AND c.session_id=p.record_id
  AND (NOT p.deleted_keys ? c.storage_key OR octet_length(c.wrapped_key)<>0
   OR octet_length(c.encrypted_data)<>0))
 OR EXISTS(SELECT 1 FROM clinic_app.ai_aiartifact a
  WHERE p.record_class IN ('ai_artifact','transcript_partial') AND a.id=p.record_id
  AND (a.content IS NOT NULL OR a.purged_at IS NULL)) THEN
  RAISE EXCEPTION 'retention targets remain' USING ERRCODE='23514'; END IF;
 UPDATE clinic_app.scribe_audiochunk SET state='purged'
 WHERE p.record_class='raw_audio' AND session_id=p.record_id AND state='purging';
 UPDATE clinic_app.retention_artifactpurge SET state='purged',
  purged_at=statement_timestamp(),failure_code='' WHERE id=p.id;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_complete(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_complete(uuid,uuid) TO clinic_agent;
RESET ROLE;
"""
