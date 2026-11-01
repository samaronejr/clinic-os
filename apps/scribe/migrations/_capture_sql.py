"""Exact FORCE RLS and recording/epoch/chunk binding and transition guards."""

_sql = r"""
GRANT SELECT ON clinic_app.scribe_recordingsession,
 clinic_app.scribe_captureepoch,clinic_app.scribe_audiochunk,
 clinic_app.scribe_recordingcommand,clinic_app.providers_providercapability,
 clinic_app.providers_capabilityversion TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.scribe_assigned(requested_session uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.scribe_recordingsession s
 WHERE s.id=requested_session
 AND s.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND s.physician_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.ehr_assigned(s.encounter_id)
 AND clinic_app.has_permission('clinical.write',s.clinic_id,s.enrollment_id))
$f$;
CREATE FUNCTION clinic_app.scribe_consent(acceptance uuid, clinic uuid, enrollment uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.consent_consentacceptance a
 JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
 WHERE a.id=acceptance AND a.clinic_id=clinic AND a.enrollment_id=enrollment
 AND t.purpose='consultation_recording'
 AND NOT EXISTS(SELECT 1 FROM clinic_app.consent_consentrevocation r
  WHERE r.acceptance_id=a.id)
 AND NOT EXISTS(SELECT 1 FROM clinic_app.consent_consenttext newer
  WHERE newer.clinic_id=t.clinic_id AND newer.purpose=t.purpose
   AND newer.version>t.version))
$f$;
CREATE FUNCTION clinic_app.scribe_gaps(
 requested_epoch uuid, requested_track text, high integer)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce(jsonb_agg(jsonb_build_array(lo,hi) ORDER BY lo),'[]'::jsonb)
 FROM (SELECT min(n) lo,max(n) hi FROM (
   SELECT n,n-row_number() OVER(ORDER BY n) AS bucket
   FROM generate_series(0,high) n WHERE NOT EXISTS(
    SELECT 1 FROM clinic_app.scribe_audiochunk c WHERE c.epoch_id=requested_epoch
    AND c.track=requested_track AND c.seq=n AND c.state IN ('buffered','accepted'))
 ) missing GROUP BY bucket) ranges
$f$;
CREATE FUNCTION clinic_app.scribe_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s clinic_app.scribe_recordingsession; e clinic_app.ehr_encounter;
 p clinic_app.intake_patientclinicenrollment; ep clinic_app.scribe_captureepoch;
 token text; item record; valid boolean;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'recording history is retained' USING ERRCODE='23514';
 END IF;
 token:=NULLIF(current_setting('app.scribe_token',true),'');
 IF TG_TABLE_NAME='scribe_recordingsession' THEN
  SELECT * INTO e FROM clinic_app.ehr_encounter WHERE id=NEW.encounter_id;
  SELECT * INTO p FROM clinic_app.intake_patientclinicenrollment
   WHERE id=NEW.enrollment_id;
  IF e.id IS NULL OR p.id IS NULL
   OR (NEW.organization_id,NEW.clinic_id,NEW.physician_id)
     IS DISTINCT FROM (e.organization_id,e.clinic_id,e.physician_id)
   OR (p.organization_id,p.clinic_id,p.patient_id)
     IS DISTINCT FROM (e.organization_id,e.clinic_id,e.patient_id)
   OR NEW.organization_id IS DISTINCT FROM
     NULLIF(current_setting('app.current_tenant',true),'')::uuid
   OR NEW.physician_id IS DISTINCT FROM
     NULLIF(current_setting('app.current_user_id',true),'')::uuid
   OR NOT clinic_app.ehr_assigned(NEW.encounter_id)
   OR NOT clinic_app.has_permission('clinical.write',NEW.clinic_id,NEW.enrollment_id)
   OR NEW.token_digest !~ '^[a-f0-9]{64}$' THEN
   RAISE EXCEPTION 'recording authority denied' USING ERRCODE='42501';
  END IF;
  IF TG_OP='INSERT' THEN
   IF NEW.state<>'created' OR NEW.epoch<>0 OR NEW.revision<>1
    OR NEW.discarded OR NEW.stopped_at IS NOT NULL OR e.state<>'open'
    OR NEW.token_digest IS DISTINCT FROM token THEN
    RAISE EXCEPTION 'invalid recording binding' USING ERRCODE='23514';
   END IF;
   NEW.created_at:=statement_timestamp();
  ELSE
   IF (to_jsonb(NEW)-
    ARRAY['state','epoch','revision','discarded','stopped_at','token_digest'])
    IS DISTINCT FROM
    (to_jsonb(OLD)-
    ARRAY['state','epoch','revision','discarded','stopped_at','token_digest'])
    OR OLD.state NOT IN ('created','authorized','recording','paused')
    OR NEW.revision<>OLD.revision+1 OR (OLD.discarded AND NOT NEW.discarded)
    OR (OLD.state,NEW.state) NOT IN (
     ('created','authorized'),('authorized','recording'),
     ('recording','paused'),('paused','recording'),
     ('authorized','paused'),('paused','paused'),
     ('created','stopped'),('authorized','stopped'),('recording','stopped'),
     ('paused','stopped'),('created','stopped_revoked'),
     ('authorized','stopped_revoked'),('recording','stopped_revoked'),
     ('paused','stopped_revoked'))
    OR (NEW.state='recording' AND NEW.epoch<>OLD.epoch+1)
    OR (NEW.state<>'recording' AND NEW.epoch<>OLD.epoch)
    OR (NEW.state NOT IN ('stopped','stopped_revoked')
      AND (NEW.stopped_at IS NOT NULL OR NEW.discarded
       OR NEW.token_digest IS DISTINCT FROM token))
    OR (NEW.state IN ('stopped','stopped_revoked') AND NEW.stopped_at IS NULL)
    OR (NEW.token_digest IS DISTINCT FROM OLD.token_digest
      AND (NEW.state<>'paused'
       OR current_setting('app.scribe_recovery',true) IS DISTINCT FROM 'true'))
   THEN RAISE EXCEPTION 'invalid recording transition' USING ERRCODE='23514';
   END IF;
  END IF;
  valid:=clinic_app.scribe_consent(NEW.consent_id,NEW.clinic_id,NEW.enrollment_id);
  IF (NEW.state IN ('created','authorized','recording') AND NOT valid)
   OR (NEW.state='stopped_revoked' AND valid)
   OR (NEW.state='recording' AND e.state<>'open')
  THEN RAISE EXCEPTION 'recording consent denied' USING ERRCODE='42501'; END IF;
  IF NEW.state IN ('created','authorized','recording') AND NOT EXISTS(
   SELECT 1 FROM clinic_app.providers_capabilityversion v
   JOIN clinic_app.providers_providercapability c ON c.id=v.capability_id
   WHERE v.id=NEW.capability_version_id AND c.key='object_storage_media'
    AND v.state NOT IN ('revoked','degraded') AND c.current_version_id=v.id
    AND NEW.capability_snapshot=jsonb_build_object(
     'capability','object_storage_media','provider',v.provider,'account',v.account,
     'environment',v.environment,'api_version',v.api_version,'region',v.region,
     'retention_terms',v.retention_terms)
    AND c.id=(SELECT chosen.id FROM clinic_app.providers_providercapability chosen
      WHERE chosen.key='object_storage_media'
       AND (chosen.clinic_id IS NULL OR chosen.clinic_id=NEW.clinic_id)
      ORDER BY chosen.clinic_id NULLS LAST LIMIT 1)
  ) THEN RAISE EXCEPTION 'recording capability denied' USING ERRCODE='42501'; END IF;
  RETURN NEW;
 END IF;
 SELECT * INTO s FROM clinic_app.scribe_recordingsession WHERE id=NEW.session_id;
 IF s.id IS NULL OR NEW.organization_id IS DISTINCT FROM s.organization_id
  OR NOT clinic_app.scribe_assigned(s.id) THEN
  RAISE EXCEPTION 'recording authority denied' USING ERRCODE='42501';
 END IF;
 IF TG_TABLE_NAME='scribe_captureepoch' THEN
  IF TG_OP='INSERT' THEN
   IF s.state<>'recording' OR NEW.number<>s.epoch OR NEW.number<1
    OR NEW.number<>coalesce((SELECT max(number)+1
      FROM clinic_app.scribe_captureepoch WHERE session_id=s.id),1)
    OR NEW.ended_at IS NOT NULL OR NEW.reason<>''
    OR NEW.high_water<>'{}'::jsonb OR NEW.gaps<>'{}'::jsonb
    OR token IS DISTINCT FROM s.token_digest THEN
    RAISE EXCEPTION 'invalid capture epoch' USING ERRCODE='23514';
   END IF;
   NEW.started_at:=statement_timestamp();
  ELSE
   IF (to_jsonb(NEW)-ARRAY['ended_at','reason','high_water','gaps'])
    IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['ended_at','reason','high_water','gaps'])
    OR OLD.ended_at IS NOT NULL OR NEW.number<>s.epoch
    OR jsonb_typeof(NEW.high_water)<>'object'
    OR jsonb_typeof(NEW.gaps)<>'object'
    OR NEW.reason NOT IN ('','user','offline','microphone','patient_switch','recovery')
    OR (NEW.ended_at IS NOT NULL AND NEW.reason='')
   THEN RAISE EXCEPTION 'invalid epoch transition' USING ERRCODE='23514'; END IF;
   FOR item IN SELECT * FROM jsonb_each(NEW.high_water) LOOP
    IF item.key NOT IN ('microphone','remote') OR jsonb_typeof(item.value)<>'number'
     OR (item.value::text)::integer NOT BETWEEN -1 AND 10799
     OR NEW.gaps->item.key IS DISTINCT FROM
      clinic_app.scribe_gaps(NEW.id,item.key,(item.value::text)::integer)
    THEN RAISE EXCEPTION 'invalid gap manifest' USING ERRCODE='23514'; END IF;
   END LOOP;
   IF NEW.ended_at IS NOT NULL THEN NEW.ended_at:=statement_timestamp(); END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='scribe_recordingcommand' THEN
  IF TG_OP<>'INSERT' OR NEW.result_state<>s.state OR NEW.result_epoch<>s.epoch
   OR NEW.action NOT IN ('start','pause','resume','stop','cancel','recover')
   OR NEW.payload_digest !~ '^[a-f0-9]{64}$' THEN
   RAISE EXCEPTION 'invalid recording receipt' USING ERRCODE='23514'; END IF;
  NEW.created_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 SELECT * INTO ep FROM clinic_app.scribe_captureepoch WHERE id=NEW.epoch_id;
 IF ep.id IS NULL OR ep.session_id<>s.id THEN
  RAISE EXCEPTION 'invalid audio epoch' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' THEN
  IF s.state<>'recording' OR ep.number<>s.epoch OR ep.ended_at IS NOT NULL
   OR NOT EXISTS(SELECT 1 FROM clinic_app.ehr_encounter
    WHERE id=s.encounter_id AND state='open')
   OR token IS DISTINCT FROM s.token_digest
   OR NOT clinic_app.scribe_consent(s.consent_id,s.clinic_id,s.enrollment_id)
   OR NOT EXISTS(
    SELECT 1 FROM clinic_app.providers_capabilityversion v
    JOIN clinic_app.providers_providercapability c ON c.id=v.capability_id
    WHERE v.id=s.capability_version_id AND c.current_version_id=v.id
    AND c.key='object_storage_media' AND v.state NOT IN ('revoked','degraded')
    AND c.id=(SELECT chosen.id FROM clinic_app.providers_providercapability chosen
     WHERE chosen.key='object_storage_media'
     AND (chosen.clinic_id IS NULL OR chosen.clinic_id=s.clinic_id)
     ORDER BY chosen.clinic_id NULLS LAST LIMIT 1))
   OR NEW.state<>'buffered' OR NEW.operation_id IS NOT NULL
   OR NEW.track NOT IN ('microphone','remote') OR NEW.seq NOT BETWEEN 0 AND 10799
   OR NEW.duration_ms NOT BETWEEN 1 AND 5000
   OR NEW.size_bytes NOT BETWEEN 1 AND 262144
   OR NEW.sha256 !~ '^[a-f0-9]{64}$' OR NEW.storage_key !~ '^[a-f0-9]{64}$'
   OR NEW.codec NOT IN ('audio/webm;codecs=opus','audio/ogg;codecs=opus')
   OR octet_length(NEW.encrypted_data) NOT BETWEEN 1 AND 270336
   OR octet_length(NEW.wrapped_key) NOT BETWEEN 1 AND 8192
   OR ep.started_at+NEW.seq*interval '2 seconds'
    >statement_timestamp()+interval '5 seconds'
  THEN RAISE EXCEPTION 'invalid audio chunk' USING ERRCODE='23514'; END IF;
  NEW.expires_at:=ep.started_at+NEW.seq*interval '2 seconds'+interval '60 seconds';
  IF NEW.expires_at<=statement_timestamp() THEN
   RAISE EXCEPTION 'expired audio chunk' USING ERRCODE='23514'; END IF;
  NEW.created_at:=statement_timestamp();
 ELSE
  IF (to_jsonb(NEW)-ARRAY['state','operation_id','encrypted_data','wrapped_key'])
   IS DISTINCT FROM
   (to_jsonb(OLD)-ARRAY['state','operation_id','encrypted_data','wrapped_key'])
  THEN RAISE EXCEPTION 'immutable audio identity' USING ERRCODE='23514'; END IF;
  IF NEW.operation_id IS DISTINCT FROM OLD.operation_id THEN
   IF OLD.operation_id IS NOT NULL OR NEW.state<>'buffered'
    OR (to_jsonb(NEW)-'operation_id') IS DISTINCT FROM (to_jsonb(OLD)-'operation_id')
    OR NOT EXISTS(SELECT 1 FROM clinic_app.comms_integrationoperation o
      WHERE o.id=NEW.operation_id AND o.kind='action' AND o.provider='scribe_media'
       AND o.subject_type='scribe.chunk' AND o.subject_id=NEW.id
       AND o.payload_digest=NEW.sha256 AND o.actor_id=s.physician_id
       AND o.organization_id=s.organization_id AND o.clinic_id=s.clinic_id)
   THEN RAISE EXCEPTION 'invalid media operation' USING ERRCODE='23514'; END IF;
  ELSIF OLD.state='buffered' AND NEW.state='accepted' THEN
   IF NEW.wrapped_key IS DISTINCT FROM OLD.wrapped_key
    OR octet_length(NEW.encrypted_data)<>0 OR s.discarded
    OR NOT clinic_app.scribe_consent(s.consent_id,s.clinic_id,s.enrollment_id)
   THEN RAISE EXCEPTION 'invalid audio acceptance' USING ERRCODE='23514'; END IF;
  ELSIF OLD.state IN ('buffered','accepted')
   AND NEW.state IN ('quarantined','rejected','purged') THEN
   IF octet_length(NEW.wrapped_key)<>0 OR octet_length(NEW.encrypted_data)<>0
    OR (NEW.state='rejected' AND NOT s.discarded)
    OR (NEW.state='purged' AND s.state IN ('created','authorized','recording','paused'))
   THEN RAISE EXCEPTION 'invalid audio retirement' USING ERRCODE='23514'; END IF;
  ELSE
   RAISE EXCEPTION 'invalid audio transition' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.scribe_consent_fence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE clinic uuid; purpose text;
BEGIN
 IF TG_TABLE_NAME='consent_consenttext' THEN
  clinic:=NEW.clinic_id; purpose:=NEW.purpose;
 ELSE
  SELECT a.clinic_id,t.purpose INTO clinic,purpose
   FROM clinic_app.consent_consentacceptance a
   JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
   WHERE a.id=NEW.acceptance_id;
 END IF;
 IF purpose='consultation_recording' THEN
  PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-lock-v1:comms-subject:scribe-consent:'||clinic,0));
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.scribe_assigned(uuid),
 clinic_app.scribe_consent(uuid,uuid,uuid),
 clinic_app.scribe_gaps(uuid,text,integer),clinic_app.scribe_guard(),
 clinic_app.scribe_consent_fence() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.scribe_assigned(uuid) TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.scribe_guard(),
 clinic_app.scribe_consent_fence() TO clinic_owner;
RESET ROLE;
"""

for _table in ("recordingsession", "captureepoch", "audiochunk", "recordingcommand"):
    _reference = "id" if _table == "recordingsession" else "session_id"
    _insert = (
        "physician_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid"
        " AND clinic_app.ehr_assigned(encounter_id)"
        if _table == "recordingsession"
        else f"clinic_app.scribe_assigned({_reference})"
    )
    _sql += f"""
ALTER TABLE clinic_app.scribe_{_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.scribe_{_table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.scribe_{_table} FROM PUBLIC,clinic_app;
GRANT SELECT,INSERT ON clinic_app.scribe_{_table} TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.scribe_{_table} TO clinic_owner
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK(
  organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY capture_read ON clinic_app.scribe_{_table} FOR SELECT TO clinic_app
 USING(clinic_app.scribe_assigned({_reference}));
CREATE POLICY capture_insert ON clinic_app.scribe_{_table} FOR INSERT TO clinic_app
 WITH CHECK(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND {_insert});
CREATE TRIGGER scribe_binding BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.scribe_{_table} FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_guard();
"""
    if _table != "recordingcommand":
        _sql += f"""
CREATE POLICY capture_update ON clinic_app.scribe_{_table} FOR UPDATE TO clinic_app
 USING(clinic_app.scribe_assigned({_reference}))
 WITH CHECK(clinic_app.scribe_assigned({_reference}));
"""

_sql += """
GRANT UPDATE(state,epoch,revision,discarded,stopped_at,token_digest)
 ON clinic_app.scribe_recordingsession TO clinic_app;
GRANT UPDATE(ended_at,reason,high_water,gaps)
 ON clinic_app.scribe_captureepoch TO clinic_app;
GRANT UPDATE(state,operation_id,encrypted_data,wrapped_key)
 ON clinic_app.scribe_audiochunk TO clinic_app;
CREATE TRIGGER scribe_recording_publication_fence
 BEFORE INSERT ON clinic_app.consent_consenttext
 FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_consent_fence();
CREATE TRIGGER scribe_recording_revocation_fence
 BEFORE INSERT ON clinic_app.consent_consentrevocation
 FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_consent_fence();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.scribe_guard(),
 clinic_app.scribe_consent_fence() FROM clinic_owner;
RESET ROLE;
"""

SQL = _sql
