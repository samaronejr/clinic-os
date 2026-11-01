"""Narrow destruction exceptions preserve the existing capture and AI guards."""

SQL = r"""

SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.retention_mutation_allowed(
 target_class text,target_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.retention_artifactpurge p
 WHERE p.record_class=target_class AND p.record_id=target_id
 AND p.state IN ('purging','purged') AND p.started_at IS NOT NULL
 AND NOT clinic_app.retention_held(p.clinic_id,p.record_class,p.record_id)
 AND (clinic_app.retention_authorized(p.clinic_id)
 OR (current_setting('app.retention_restore',true)='true'
 AND (current_setting('role')='clinic_owner' OR session_user='clinic_owner'
 OR EXISTS(SELECT 1 FROM pg_roles WHERE rolname=session_user AND rolsuper)))))
$f$;
REVOKE ALL ON FUNCTION clinic_app.retention_mutation_allowed(text,uuid) FROM PUBLIC;
CREATE FUNCTION clinic_app.retention_shred(requested uuid)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE p clinic_app.retention_artifactpurge;
BEGIN
 SELECT * INTO p FROM clinic_app.retention_artifactpurge WHERE id=requested;
 IF p.id IS NULL OR NOT clinic_app.retention_mutation_allowed(
  p.record_class,p.record_id) THEN
  RAISE EXCEPTION 'retention denied' USING ERRCODE='42501'; END IF;
 UPDATE clinic_app.scribe_audiochunk SET wrapped_key=''::bytea,
  encrypted_data=''::bytea,state=CASE WHEN p.state='purged' THEN 'purged'
   ELSE 'purging' END
 WHERE p.record_class='raw_audio' AND session_id=p.record_id;
 UPDATE clinic_app.ai_aiartifact SET content=NULL,
  purged_at=coalesce(purged_at,p.started_at)
 WHERE p.record_class IN ('ai_artifact','transcript_partial') AND id=p.record_id;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_shred(uuid) FROM PUBLIC;

CREATE OR REPLACE FUNCTION clinic_app.scribe_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s clinic_app.scribe_recordingsession; e clinic_app.ehr_encounter;
 p clinic_app.intake_patientclinicenrollment; ep clinic_app.scribe_captureepoch;
 token text; item record; valid boolean;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'recording history is retained' USING ERRCODE='23514';
 END IF;

 IF TG_TABLE_NAME='scribe_audiochunk' AND TG_OP='UPDATE' THEN
  IF clinic_app.retention_mutation_allowed('raw_audio',NEW.session_id) THEN
   IF (to_jsonb(NEW)-ARRAY['state','wrapped_key','encrypted_data'])
    IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','wrapped_key','encrypted_data'])
    OR octet_length(NEW.wrapped_key)<>0 OR octet_length(NEW.encrypted_data)<>0
    OR NEW.state NOT IN ('purging','purged') THEN
    RAISE EXCEPTION 'invalid retention destruction' USING ERRCODE='23514';
   END IF;
   RETURN NEW;
  END IF;
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
  valid:=clinic_app.scribe_consent(NEW.consent_id,NEW.clinic_id,NEW.enrollment_id,NEW.encounter_id);
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
   OR NOT clinic_app.scribe_consent(
    s.consent_id,s.clinic_id,s.enrollment_id,s.encounter_id)
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
   OR NEW.storage_location !~ '^[a-f0-9]{64}$'
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
    OR NOT clinic_app.scribe_consent(
     s.consent_id,s.clinic_id,s.enrollment_id,s.encounter_id)
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
CREATE OR REPLACE FUNCTION clinic_app.ai_ledger_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE source clinic_app.ai_aiartifact; config clinic_app.ai_modelconfiguration;
 invocation clinic_app.ai_aiinvocation; policy jsonb; usage record;
 reservation bigint; amount bigint;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'AI history is retained' USING ERRCODE='23514';
 END IF;

 IF TG_TABLE_NAME='ai_aiartifact' AND TG_OP='UPDATE' THEN
  IF NOT clinic_app.retention_mutation_allowed(CASE
   WHEN OLD.retention_class='transcript_partial' THEN 'transcript_partial'
   ELSE 'ai_artifact' END,OLD.id)
  OR (to_jsonb(NEW)-ARRAY['content','purged_at']) IS DISTINCT FROM
   (to_jsonb(OLD)-ARRAY['content','purged_at'])
  OR NEW.content IS NOT NULL OR NEW.purged_at IS NULL
  OR (OLD.purged_at IS NOT NULL AND NEW.purged_at<>OLD.purged_at) THEN
   RAISE EXCEPTION 'AI destruction denied' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;

 IF TG_TABLE_NAME='ai_aiartifact' THEN
  IF TG_OP<>'INSERT' OR NEW.user_id IS DISTINCT FROM
   NULLIF(current_setting('app.current_user_id',true),'')::uuid
  OR NOT clinic_app.ai_staff(NEW.clinic_id,NEW.purpose,NEW.patient_enrollment_id)
  OR NEW.purged_at IS NOT NULL
  OR NEW.content IS NULL OR octet_length(NEW.content)>1056768
  OR NEW.expires_at<=statement_timestamp() THEN
   RAISE EXCEPTION 'AI artifact denied' USING ERRCODE='23514';
  END IF;
  IF NEW.kind='output' THEN
   SELECT * INTO invocation FROM clinic_app.ai_aiinvocation
    WHERE id=NEW.invocation_id;
   SELECT * INTO source FROM clinic_app.ai_aiartifact
    WHERE id=invocation.inputs_ref_id;
   IF invocation.id IS NULL OR invocation.status<>'reserved'
   OR source.purged_at IS NOT NULL
   OR (NEW.organization_id,NEW.clinic_id,NEW.user_id,NEW.purpose)
    IS DISTINCT FROM (invocation.organization_id,invocation.clinic_id,
     invocation.user_id,invocation.purpose)
   OR NEW.patient_enrollment_id IS DISTINCT FROM source.patient_enrollment_id
   OR NEW.method<>source.method THEN
    RAISE EXCEPTION 'AI artifact binding denied' USING ERRCODE='23514';
   END IF;
  END IF;
  NEW.created_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='ai_aicircuit' THEN
  IF TG_OP='UPDATE' THEN
   IF NOT (current_setting('role')='clinic_owner'
    OR (current_setting('role')='none' AND session_user='clinic_owner'))
   OR (to_jsonb(NEW)-'closed_at') IS DISTINCT FROM (to_jsonb(OLD)-'closed_at')
   OR OLD.closed_at IS NOT NULL OR NEW.closed_at IS NULL THEN
    RAISE EXCEPTION 'AI circuit transition denied' USING ERRCODE='23514';
   END IF;
   NEW.closed_at:=statement_timestamp();
  ELSE
   IF NEW.closed_at IS NOT NULL OR NOT EXISTS (
    SELECT 1 FROM clinic_app.ai_aiinvocation i
    WHERE i.organization_id=NEW.organization_id
     AND i.configuration_id=NEW.configuration_id
     AND i.status='failed' AND i.reason_code='provider_timeout'
     AND i.user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
     AND i.settled_at>coalesce((
      SELECT max(prior.closed_at) FROM clinic_app.ai_aicircuit prior
      WHERE prior.organization_id=NEW.organization_id
       AND prior.configuration_id=NEW.configuration_id
     ),'-infinity'::timestamptz)
   ) THEN RAISE EXCEPTION 'AI circuit binding denied' USING ERRCODE='23514';
   END IF;
   NEW.opened_at:=statement_timestamp();
  END IF;
  RETURN NEW;
 END IF;
 SELECT * INTO source FROM clinic_app.ai_aiartifact WHERE id=NEW.inputs_ref_id;
 SELECT * INTO config FROM clinic_app.ai_modelconfiguration
  WHERE id=NEW.configuration_id;
 IF source.id IS NULL OR config.id IS NULL
 OR (NEW.organization_id,NEW.clinic_id,NEW.user_id,NEW.purpose)
  IS DISTINCT FROM
  (source.organization_id,source.clinic_id,source.user_id,source.purpose)
 OR NEW.config_version<>config.version OR NEW.purpose<>config.purpose
 OR NEW.user_id IS DISTINCT FROM
   NULLIF(current_setting('app.current_user_id',true),'')::uuid
 OR NEW.prompt_sha256 !~ '^[a-f0-9]{64}$' THEN
  RAISE EXCEPTION 'AI invocation binding denied' USING ERRCODE='23514';
 END IF;
 IF TG_OP='INSERT' THEN
  PERFORM pg_advisory_xact_lock(hashtextextended(
    'ai-budget:'||NEW.organization_id,0));
  policy:=clinic_app.ai_organization_policy();
  reservation:=NEW.limit_tokens_in::bigint*
   greatest(config.input_micros,config.cached_micros)
   +NEW.limit_tokens_out::bigint*config.output_micros
   +NEW.limit_audio_seconds::bigint*config.audio_micros;
  SELECT * INTO usage FROM clinic_app.ai_budget_usage(NEW.purpose);
  IF NEW.status<>'reserved' OR NEW.operation_id IS NOT NULL
  OR NEW.tokens_in<>0 OR NEW.tokens_out<>0 OR NEW.tokens_cached<>0
  OR NEW.audio_seconds<>0 OR NEW.cost_micros<>0 OR NEW.cost_minor<>0
  OR NEW.latency_ms<>0 OR NEW.reason_code<>'' OR NEW.settled_at IS NOT NULL
  OR source.kind<>'input' OR source.expires_at<=statement_timestamp()
  OR NOT clinic_app.ai_staff(source.clinic_id,source.purpose,
   source.patient_enrollment_id)
  OR config.state<>'active' OR config.approval_ref=''
  OR NEW.reserved_micros<>reservation
  OR NOT coalesce((policy->>'enabled')::boolean,false)
  OR NOT (policy->'purposes') ? NEW.purpose
  OR coalesce(usage.total_micros+reservation>
   (policy->>'monthly_budget_micros')::numeric,true)
  OR coalesce(usage.purpose_micros+reservation>
   (policy->'purpose_budgets'->>NEW.purpose)::numeric,true)
  OR NOT EXISTS (SELECT 1 FROM clinic_app.ai_gatewayswitch g
    WHERE g.id=1 AND g.enabled AND g.approved_regions ? config.region)
  THEN RAISE EXCEPTION 'AI reservation denied' USING ERRCODE='23514'; END IF;
  NEW.created_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF (to_jsonb(NEW)-ARRAY['operation_id','status','reason_code','tokens_in',
  'tokens_out','tokens_cached','audio_seconds','cost_micros','cost_minor',
  'latency_ms','settled_at']) IS DISTINCT FROM
  (to_jsonb(OLD)-ARRAY['operation_id','status','reason_code','tokens_in',
   'tokens_out','tokens_cached','audio_seconds','cost_micros','cost_minor',
   'latency_ms','settled_at']) OR OLD.status<>'reserved' THEN
  RAISE EXCEPTION 'AI invocation immutable terms' USING ERRCODE='23514';
 END IF;
 IF NEW.operation_id IS DISTINCT FROM OLD.operation_id THEN
  IF OLD.operation_id IS NOT NULL OR NEW.status<>'reserved'
  OR (to_jsonb(NEW)-'operation_id') IS DISTINCT FROM
    (to_jsonb(OLD)-'operation_id')
  OR NOT EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
   WHERE o.id=NEW.operation_id AND o.kind='action' AND o.provider='ai_gateway'
    AND o.subject_type='ai.invocation' AND o.subject_id=NEW.id
    AND o.clinic_id=NEW.clinic_id AND o.organization_id=NEW.organization_id
    AND o.actor_id=NEW.user_id AND o.payload_digest=NEW.prompt_sha256)
  THEN RAISE EXCEPTION 'AI outbox binding denied' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 amount:=(NEW.tokens_in-NEW.tokens_cached)::bigint*config.input_micros
  +NEW.tokens_cached::bigint*config.cached_micros
  +NEW.tokens_out::bigint*config.output_micros
  +NEW.audio_seconds::bigint*config.audio_micros;
 IF (NEW.status='succeeded' AND (NEW.reason_code<>'' OR NEW.cost_micros<>amount
  OR NOT EXISTS (SELECT 1 FROM clinic_app.ai_aiartifact a
   WHERE a.invocation_id=NEW.id AND a.kind='output')))
 OR (NEW.status='failed' AND (
  NEW.reason_code<>ALL(ARRAY['disabled','budget_exhausted',
  'configuration_unavailable','circuit_open','provider_timeout',
  'provider_unavailable','invalid_output','authority_revoked'])
  OR NEW.cost_micros<>CASE WHEN NEW.reason_code=ANY(ARRAY[
   'provider_timeout','provider_unavailable','invalid_output'])
   THEN OLD.reserved_micros ELSE 0 END))
 OR NEW.status NOT IN ('succeeded','failed')
 OR NEW.cost_minor<>(NEW.cost_micros+9999)/10000 THEN
  RAISE EXCEPTION 'AI terminal receipt denied' USING ERRCODE='23514';
 END IF;
 NEW.settled_at:=statement_timestamp();
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.retention_purge_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'purge history is retained' USING ERRCODE='23514'; END IF;
 IF current_setting('app.retention_restore',true)='true'
 AND (current_setting('role')='clinic_owner' OR session_user='clinic_owner'
 OR EXISTS(SELECT 1 FROM pg_roles WHERE rolname=session_user AND rolsuper))
 AND NEW.state IN ('purging','purged') AND NEW.started_at IS NOT NULL
 AND NEW.backup_copy_expires_at IS NOT NULL THEN RETURN NEW; END IF;
 IF NOT clinic_app.retention_authorized(NEW.clinic_id)
 OR NEW.organization_id IS DISTINCT FROM
  NULLIF(current_setting('app.current_tenant',true),'')::uuid THEN
  RAISE EXCEPTION 'retention denied' USING ERRCODE='42501'; END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.principal_id IS DISTINCT FROM
   NULLIF(current_setting('app.current_principal',true),'')::uuid
  OR NEW.state NOT IN ('purge_due','blocked_hold') OR NEW.started_at IS NOT NULL
  OR NEW.purged_at IS NOT NULL OR NEW.backup_copy_expires_at IS NOT NULL
  OR NEW.target_count<>0 OR NEW.deleted_keys<>'[]'::jsonb OR NEW.failure_code<>''
  OR NEW.failure_task_id IS NOT NULL
  OR NEW.due_at IS DISTINCT FROM clinic_app.retention_due(
   NEW.clinic_id,NEW.record_class,NEW.record_id)
  OR NEW.due_at>statement_timestamp() THEN
   RAISE EXCEPTION 'invalid purge binding' USING ERRCODE='23514'; END IF;
 ELSE
  IF (to_jsonb(NEW)-ARRAY['state','started_at','purged_at','backup_copy_expires_at',
   'target_count','deleted_keys','failure_code','failure_task_id'])
   IS DISTINCT FROM
   (to_jsonb(OLD)-ARRAY['state','started_at','purged_at','backup_copy_expires_at',
    'target_count','deleted_keys','failure_code','failure_task_id'])
  OR OLD.state='purged' OR (OLD.state,NEW.state) NOT IN (
   ('purge_due','purging'),('purge_due','blocked_hold'),
   ('blocked_hold','purge_due'),('blocked_hold','blocked_hold'),
   ('blocked_hold','purging'),('purging','purging'),('purging','purged'),
   ('purging','blocked_hold'))
  OR (OLD.started_at IS NOT NULL AND
   (NEW.started_at,NEW.backup_copy_expires_at,NEW.target_count) IS DISTINCT FROM
   (OLD.started_at,OLD.backup_copy_expires_at,OLD.target_count))
  OR NOT NEW.deleted_keys @> OLD.deleted_keys
  OR (OLD.failure_task_id IS NOT NULL
   AND NEW.failure_task_id IS DISTINCT FROM OLD.failure_task_id) THEN
   RAISE EXCEPTION 'invalid purge transition' USING ERRCODE='23514'; END IF;
 END IF;
 IF NEW.state IN ('purging','purged') AND (
  NEW.started_at IS NULL OR NEW.backup_copy_expires_at IS NULL
  OR clinic_app.retention_held(NEW.clinic_id,NEW.record_class,NEW.record_id))
 OR (NEW.state='purged' AND (NEW.purged_at IS NULL
  OR jsonb_array_length(NEW.deleted_keys)<>NEW.target_count)) THEN
  RAISE EXCEPTION 'incomplete purge receipt' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_purge_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_purge_guard() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER retention_purge_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.retention_artifactpurge FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_purge_guard();
CREATE TRIGGER retention_purge_no_truncate BEFORE TRUNCATE
 ON clinic_app.retention_artifactpurge FOR EACH STATEMENT
 EXECUTE FUNCTION clinic_app.retention_purge_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.retention_purge_guard() FROM clinic_owner;
RESET ROLE;

"""
