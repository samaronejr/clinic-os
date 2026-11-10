"""Frozen source-derived, idempotent metering and server admission."""

SQL = r"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.subscription_quota(
 requested_org uuid, requested_meter text, excluded_invocation uuid DEFAULT NULL)
RETURNS TABLE(used bigint, quota bigint, enabled boolean, state_allowed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE sub clinic_app.subscriptions_subscription; allowance
 clinic_app.subscriptions_entitlement;
 current_month date; reserved numeric;
BEGIN
 SELECT * INTO sub FROM clinic_app.subscriptions_subscription
 WHERE organization_id=requested_org FOR UPDATE;
 SELECT * INTO allowance FROM clinic_app.subscriptions_entitlement
 WHERE plan_id=sub.plan_id AND meter=requested_meter;
 current_month:=date_trunc('month',clinic_app.subscription_clock() AT TIME ZONE
  'UTC')::date;
 SELECT COALESCE(sum(r.quantity),0) INTO used
 FROM clinic_app.subscriptions_usagerollup r
 WHERE r.organization_id=requested_org AND r.meter=requested_meter AND
  r.period=current_month;
 IF requested_meter='seats' THEN
  SELECT count(*) INTO used FROM clinic_app.subscriptions_seat s
  JOIN clinic_app.identity_userclinicrole m ON m.id=s.membership_id
  WHERE s.organization_id=requested_org AND m.revoked_at IS NULL;
 END IF;
 SELECT COALESCE(sum(CASE requested_meter
  WHEN 'ai_tokens_in' THEN i.limit_tokens_in
  WHEN 'ai_tokens_out' THEN i.limit_tokens_out
  WHEN 'ai_tokens_cached' THEN i.limit_tokens_in
  WHEN 'ai_audio_seconds' THEN i.limit_audio_seconds
  WHEN 'ai_cost_micros' THEN i.reserved_micros ELSE 0 END),0)
 INTO reserved FROM clinic_app.ai_aiinvocation i
 WHERE i.organization_id=requested_org AND i.status='reserved'
 AND (excluded_invocation IS NULL OR i.id<>excluded_invocation)
 AND date_trunc('month',i.created_at AT TIME ZONE 'UTC')=current_month;
 used:=used+reserved;
 quota:=COALESCE(allowance.quota,0);
 enabled:=COALESCE(allowance.enabled,false);
 state_allowed:=COALESCE(
  sub.state='active'
  OR (sub.state='trial' AND clinic_app.subscription_clock()<sub.trial_ends_at)
  OR (sub.state IN ('past_due','grace') AND
   clinic_app.subscription_clock()<sub.grace_ends_at)
  OR (sub.state='cancelled' AND
   clinic_app.subscription_clock()<sub.cancellation_at),false);
 RETURN NEXT;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_quota(uuid,text,uuid) FROM PUBLIC;

CREATE FUNCTION clinic_app.subscription_allowance(
 requested_clinic uuid, requested_meter text, requested_quantity bigint)
RETURNS TABLE(available boolean, used bigint, quota bigint, reason text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE org uuid; allowance record;
BEGIN
 org:=clinic_app.subscription_scope(requested_clinic,false);
 IF org IS NULL THEN RETURN; END IF;
 IF requested_meter IN ('clinical_core','record_read','record_export',
  'reconciliation') THEN
  RETURN QUERY SELECT true,0::bigint,0::bigint,'continuity'::text;
  RETURN;
 END IF;
 SELECT * INTO allowance FROM clinic_app.subscription_quota(org,requested_meter);
 available:=COALESCE(requested_quantity>=0 AND allowance.enabled
  AND allowance.state_allowed AND
   requested_quantity<=allowance.quota-allowance.used,false);
 used:=allowance.used; quota:=allowance.quota;
 reason:=CASE WHEN available THEN 'available'
  WHEN NOT allowance.state_allowed THEN 'subscription_restricted'
  WHEN NOT allowance.enabled THEN 'feature_unavailable' ELSE 'quota_exhausted' END;
 RETURN NEXT;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_allowance(uuid,text,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.subscription_allowance(uuid,text,bigint) TO
 clinic_app;

CREATE FUNCTION clinic_app.subscription_entitled(
 requested_clinic uuid, requested_meter text, requested_quantity bigint)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT COALESCE((SELECT available FROM clinic_app.subscription_allowance(
  requested_clinic,requested_meter,requested_quantity)),false)
$f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_entitled(uuid,text,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.subscription_entitled(uuid,text,bigint) TO
 clinic_app;

CREATE FUNCTION clinic_app.subscription_record_usage(
 requested_org uuid, requested_meter text, requested_quantity bigint,
 happened_at timestamptz, source_id uuid)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE inserted_id uuid; original clinic_app.subscriptions_usageevent; month_start
 date;
BEGIN
 IF requested_quantity=0 THEN RETURN; END IF;
 month_start:=date_trunc('month',happened_at AT TIME ZONE 'UTC')::date;
 INSERT INTO clinic_app.subscriptions_usageevent
 (id,organization_id,meter,quantity,occurred_at,source_ref)
 VALUES (gen_random_uuid(),requested_org,requested_meter,requested_quantity,
  happened_at,source_id)
 ON CONFLICT (organization_id,meter,source_ref) DO NOTHING RETURNING id INTO
  inserted_id;
 IF inserted_id IS NULL THEN
  SELECT * INTO original FROM clinic_app.subscriptions_usageevent
  WHERE organization_id=requested_org AND meter=requested_meter AND
   source_ref=source_id;
  IF original.quantity IS DISTINCT FROM requested_quantity OR original.occurred_at
   IS DISTINCT FROM happened_at THEN
   RAISE EXCEPTION 'usage idempotency conflict' USING ERRCODE='23514';
  END IF;
  RETURN;
 END IF;
 INSERT INTO clinic_app.subscriptions_usagerollup (id,organization_id,meter,period,
  quantity)
 VALUES (gen_random_uuid(),requested_org,requested_meter,month_start,requested_quantity)
 ON CONFLICT (organization_id,meter,period) DO UPDATE
 SET quantity=clinic_app.subscriptions_usagerollup.quantity+EXCLUDED.quantity;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_record_usage(uuid,text,bigint,
 timestamptz,uuid) FROM PUBLIC;

CREATE FUNCTION clinic_app.subscription_ai_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE allowance record; requested record;
BEGIN
 FOR requested IN SELECT * FROM (VALUES
  ('ai_tokens_in',NEW.limit_tokens_in::bigint),
  ('ai_tokens_out',NEW.limit_tokens_out::bigint),
  ('ai_tokens_cached',NEW.limit_tokens_in::bigint),
  ('ai_audio_seconds',NEW.limit_audio_seconds::bigint),
  ('ai_cost_micros',NEW.reserved_micros::bigint)) q(meter,quantity)
 LOOP
  SELECT * INTO allowance FROM clinic_app.subscription_quota(NEW.organization_id,
   requested.meter);
  IF NOT allowance.enabled OR NOT allowance.state_allowed
   OR requested.quantity>allowance.quota-allowance.used THEN
   RAISE EXCEPTION 'subscription allowance unavailable' USING ERRCODE='23514';
  END IF;
 END LOOP;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_ai_guard() FROM PUBLIC;

CREATE FUNCTION clinic_app.subscription_source_meter()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_TABLE_NAME='ai_aiinvocation' THEN
  IF NEW.status NOT IN ('succeeded','failed') OR OLD.status<>'reserved' THEN RETURN
   NEW; END IF;
  PERFORM clinic_app.subscription_record_usage(NEW.organization_id,'ai_tokens_in',
   NEW.tokens_in,NEW.settled_at,NEW.id);
  PERFORM clinic_app.subscription_record_usage(NEW.organization_id,'ai_tokens_out',
   NEW.tokens_out,NEW.settled_at,NEW.id);
  PERFORM clinic_app.subscription_record_usage(NEW.organization_id,
   'ai_tokens_cached',NEW.tokens_cached,NEW.settled_at,NEW.id);
  PERFORM clinic_app.subscription_record_usage(NEW.organization_id,
   'ai_audio_seconds',NEW.audio_seconds,NEW.settled_at,NEW.id);
  PERFORM clinic_app.subscription_record_usage(NEW.organization_id,'ai_cost_micros',
   NEW.cost_micros,NEW.settled_at,NEW.id);
 ELSIF TG_TABLE_NAME='teleconsult_teleconsultsession' THEN
  IF NEW.state IN ('ended','failed') AND NEW.started_at IS NOT NULL
   AND OLD.state NOT IN ('ended','failed') THEN
   PERFORM clinic_app.subscription_record_usage(NEW.organization_id,'video_seconds',
    (ceil(greatest(0,extract(epoch FROM NEW.ended_at-NEW.started_at))) * 2)::bigint,
    NEW.ended_at,NEW.id);
  END IF;
 ELSIF TG_TABLE_NAME='comms_message' THEN
  IF NEW.direction='out' AND NEW.provider_state IN ('sent','delivered','read') THEN
   PERFORM clinic_app.subscription_record_usage(NEW.organization_id,'messages',
    1::bigint,NEW.created_at,NEW.id);
  END IF;
 ELSIF TG_TABLE_NAME='ehr_clinicalattachment' THEN
  PERFORM clinic_app.subscription_record_usage(NEW.organization_id,'storage_bytes',
   NEW.size_bytes::bigint,NEW.created_at,NEW.id);
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_source_meter() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.subscription_ai_guard(),
 clinic_app.subscription_source_meter() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER subscription_ai_admission BEFORE INSERT ON clinic_app.ai_aiinvocation
 FOR EACH ROW EXECUTE FUNCTION clinic_app.subscription_ai_guard();
CREATE TRIGGER subscription_ai_usage AFTER UPDATE ON clinic_app.ai_aiinvocation
 FOR EACH ROW EXECUTE FUNCTION clinic_app.subscription_source_meter();
CREATE TRIGGER subscription_video_usage AFTER UPDATE ON
 clinic_app.teleconsult_teleconsultsession
 FOR EACH ROW EXECUTE FUNCTION clinic_app.subscription_source_meter();
CREATE TRIGGER subscription_message_usage AFTER INSERT OR UPDATE ON
 clinic_app.comms_message
 FOR EACH ROW EXECUTE FUNCTION clinic_app.subscription_source_meter();
CREATE TRIGGER subscription_storage_usage AFTER INSERT ON
 clinic_app.ehr_clinicalattachment
 FOR EACH ROW EXECUTE FUNCTION clinic_app.subscription_source_meter();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.subscription_ai_guard(),
 clinic_app.subscription_source_meter() FROM clinic_owner;
RESET ROLE;
"""

REVERSE = """
DROP TRIGGER subscription_storage_usage ON clinic_app.ehr_clinicalattachment;
DROP TRIGGER subscription_message_usage ON clinic_app.comms_message;
DROP TRIGGER subscription_video_usage ON clinic_app.teleconsult_teleconsultsession;
DROP TRIGGER subscription_ai_usage ON clinic_app.ai_aiinvocation;
DROP TRIGGER subscription_ai_admission ON clinic_app.ai_aiinvocation;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.subscription_source_meter();
DROP FUNCTION clinic_app.subscription_ai_guard();
DROP FUNCTION clinic_app.subscription_record_usage(uuid,text,bigint,timestamptz,uuid);
DROP FUNCTION clinic_app.subscription_entitled(uuid,text,bigint);
DROP FUNCTION clinic_app.subscription_allowance(uuid,text,bigint);
DROP FUNCTION clinic_app.subscription_quota(uuid,text,uuid);
RESET ROLE;
"""
