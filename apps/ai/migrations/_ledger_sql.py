"""DB-enforced reservations, append-only artifacts, and terminal receipts."""

SQL = r"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_ledger_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE source clinic_app.ai_aiartifact; config clinic_app.ai_modelconfiguration;
 invocation clinic_app.ai_aiinvocation; policy jsonb; usage record;
 reservation bigint; amount bigint;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'AI history is retained' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='ai_aiartifact' THEN
  IF TG_OP<>'INSERT' OR NEW.user_id IS DISTINCT FROM
   NULLIF(current_setting('app.current_user_id',true),'')::uuid
  OR NOT clinic_app.ai_staff(NEW.clinic_id,NEW.purpose,NEW.patient_enrollment_id)
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
REVOKE ALL ON FUNCTION clinic_app.ai_ledger_guard() FROM PUBLIC;
RESET ROLE;
CREATE TRIGGER ai_artifact_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.ai_aiartifact FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_ledger_guard();
CREATE TRIGGER ai_invocation_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.ai_aiinvocation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_ledger_guard();
CREATE TRIGGER ai_circuit_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.ai_aicircuit FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_ledger_guard();
"""
