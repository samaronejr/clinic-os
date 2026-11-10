"""Frozen commercial authority, lifecycle, seats and exact table posture."""

TABLES = (
    "subscriptions_plan",
    "subscriptions_entitlement",
    "subscriptions_subscription",
    "subscriptions_subscriptionevent",
    "subscriptions_seat",
    "subscriptions_usageevent",
    "subscriptions_usagerollup",
)

SQL = r"""
GRANT SELECT, INSERT ON clinic_app.subscriptions_plan,
 clinic_app.subscriptions_entitlement, clinic_app.subscriptions_subscription,
 clinic_app.subscriptions_subscriptionevent, clinic_app.subscriptions_seat,
 clinic_app.subscriptions_usageevent, clinic_app.subscriptions_usagerollup TO
  clinic_resolver;
GRANT UPDATE (plan_id,state,revision,grace_ends_at,cancellation_at)
 ON clinic_app.subscriptions_subscription TO clinic_resolver;
GRANT UPDATE (quantity) ON clinic_app.subscriptions_usagerollup TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;

CREATE FUNCTION clinic_app.subscription_clock()
RETURNS timestamptz LANGUAGE sql VOLATILE
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT statement_timestamp()
$f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_clock() FROM PUBLIC;

CREATE FUNCTION clinic_app.subscription_scope(requested_clinic uuid, manage boolean)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT c.organization_id FROM clinic_app.identity_clinic c
 WHERE c.id=requested_clinic
 AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.user_has_org(c.organization_id)
 AND (NOT manage OR NOT EXISTS (
  SELECT 1 FROM clinic_app.identity_clinic unit
  WHERE unit.organization_id=c.organization_id
  AND NOT clinic_app.has_permission('configuration.organization',unit.id,NULL)))
$f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_scope(uuid,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.subscription_scope(uuid,boolean) TO clinic_app,
 clinic_owner;

CREATE FUNCTION clinic_app.subscription_seats(requested_org uuid)
RETURNS bigint LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT count(*) FROM clinic_app.subscriptions_seat s
 JOIN clinic_app.identity_userclinicrole m ON m.id=s.membership_id
 WHERE s.organization_id=requested_org AND m.revoked_at IS NULL
 AND requested_org=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.user_has_org(requested_org)
$f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_seats(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.subscription_seats(uuid) TO clinic_app,
 clinic_owner;

CREATE FUNCTION clinic_app.subscription_bootstrap(requested_org uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE selected_plan uuid; selected_subscription uuid;
BEGIN
 -- Serializes first package creation across concurrently created organizations.
 PERFORM pg_advisory_xact_lock(hashtextextended('subscriptions-packaging-v1',0));
 INSERT INTO clinic_app.subscriptions_plan
 (id,code,version,provisional,monthly_price_minor)
 SELECT gen_random_uuid(),code,1,true,NULL FROM unnest(ARRAY[
  'core_operations','intelligence_usage','network_governance']) code
 ON CONFLICT (code,version) DO NOTHING;
 INSERT INTO clinic_app.subscriptions_entitlement (id,plan_id,meter,enabled,quota)
 SELECT gen_random_uuid(),p.id,m.meter,
  (p.code<>'core_operations' OR m.meter NOT LIKE 'ai_%'),m.quota
 FROM clinic_app.subscriptions_plan p CROSS JOIN (VALUES
  ('ai_tokens_in',10000000::bigint),('ai_tokens_out',10000000),
  ('ai_tokens_cached',10000000),('ai_audio_seconds',360000),
  ('ai_cost_micros',1000000000),('video_seconds',360000),
  ('messages',10000),('storage_bytes',10737418240),('seats',10000)
 ) m(meter,quota) WHERE p.version=1 AND p.provisional
 ON CONFLICT (plan_id,meter) DO NOTHING;
 SELECT id INTO selected_plan FROM clinic_app.subscriptions_plan
 WHERE code='intelligence_usage' ORDER BY version DESC LIMIT 1;
 INSERT INTO clinic_app.subscriptions_subscription
 (id,organization_id,plan_id,state,revision,trial_ends_at,grace_ends_at,
  cancellation_at,created_at)
 VALUES (gen_random_uuid(),requested_org,selected_plan,'trial',1,
  clinic_app.subscription_clock()+interval '30 days',NULL,NULL,
   clinic_app.subscription_clock())
 ON CONFLICT (organization_id) DO NOTHING RETURNING id INTO selected_subscription;
 IF selected_subscription IS NOT NULL THEN
  INSERT INTO clinic_app.subscriptions_subscriptionevent
  (id,organization_id,subscription_id,command_id,action,plan_code,from_state,
   to_state,revision,occurred_at)
  VALUES (gen_random_uuid(),requested_org,selected_subscription,selected_subscription,
   'trial_started','intelligence_usage','','trial',1,clinic_app.subscription_clock());
 END IF;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_bootstrap(uuid) FROM PUBLIC;

CREATE FUNCTION clinic_app.subscription_identity_hook()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE sub clinic_app.subscriptions_subscription; occupied bigint; allowance bigint;
BEGIN
 IF TG_TABLE_NAME='identity_organization' THEN
  PERFORM clinic_app.subscription_bootstrap(NEW.id);
  RETURN NEW;
 END IF;
 SELECT * INTO sub FROM clinic_app.subscriptions_subscription
 WHERE organization_id=NEW.organization_id FOR UPDATE;
 IF sub.id IS NULL THEN
  RAISE EXCEPTION 'subscription unavailable' USING ERRCODE='23514';
 END IF;
 IF NEW.revoked_at IS NULL THEN
  SELECT count(*) INTO occupied FROM clinic_app.subscriptions_seat s
  JOIN clinic_app.identity_userclinicrole m ON m.id=s.membership_id
  WHERE s.organization_id=NEW.organization_id AND m.revoked_at IS NULL;
  SELECT quota INTO allowance FROM clinic_app.subscriptions_entitlement
  WHERE plan_id=sub.plan_id AND meter='seats' AND enabled;
  IF occupied>=COALESCE(allowance,0) THEN
   RAISE EXCEPTION 'subscription seat quota reached' USING ERRCODE='23514';
  END IF;
 END IF;
 INSERT INTO clinic_app.subscriptions_seat
 (id,organization_id,subscription_id,membership_id,created_at)
 VALUES (gen_random_uuid(),NEW.organization_id,sub.id,NEW.id,
  clinic_app.subscription_clock());
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_identity_hook() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.subscription_identity_hook() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER subscription_organization_created AFTER INSERT ON
 clinic_app.identity_organization
 FOR EACH ROW EXECUTE FUNCTION clinic_app.subscription_identity_hook();
CREATE TRIGGER subscription_membership_created AFTER INSERT ON
 clinic_app.identity_userclinicrole
 FOR EACH ROW EXECUTE FUNCTION clinic_app.subscription_identity_hook();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.subscription_identity_hook() FROM clinic_owner;
SELECT clinic_app.subscription_bootstrap(id) FROM clinic_app.identity_organization;
INSERT INTO clinic_app.subscriptions_seat
 (id,organization_id,subscription_id,membership_id,created_at)
SELECT gen_random_uuid(),m.organization_id,s.id,m.id,clinic_app.subscription_clock()
FROM clinic_app.identity_userclinicrole m JOIN clinic_app.subscriptions_subscription s
 ON s.organization_id=m.organization_id;
SET CONSTRAINTS ALL IMMEDIATE;

CREATE FUNCTION clinic_app.subscription_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'subscription history is retained' USING ERRCODE='23514';
 END IF;
 IF TG_OP='UPDATE' THEN
  IF TG_TABLE_NAME='subscriptions_usagerollup' THEN
   IF (to_jsonb(NEW)-'quantity')=(to_jsonb(OLD)-'quantity')
    AND NEW.quantity>=OLD.quantity THEN RETURN NEW; END IF;
  END IF;
  IF TG_TABLE_NAME='subscriptions_subscription' THEN
   IF (to_jsonb(NEW)-ARRAY['plan_id','state','revision','grace_ends_at',
    'cancellation_at'])
    =(to_jsonb(OLD)-ARRAY['plan_id','state','revision','grace_ends_at',
     'cancellation_at'])
    AND NEW.revision=OLD.revision+1
    AND ((OLD.state=NEW.state AND OLD.state<>'offboarded')
      OR (OLD.state='trial' AND NEW.state IN ('active','restricted','cancelled'))
      OR (OLD.state='active' AND NEW.state IN ('past_due','cancelled'))
      OR (OLD.state='past_due' AND NEW.state IN ('active','grace','cancelled'))
      OR (OLD.state='grace' AND NEW.state IN ('active','restricted','cancelled'))
      OR (OLD.state='restricted' AND NEW.state IN ('active','cancelled'))
      OR (OLD.state='cancelled' AND NEW.state='offboarded')) THEN RETURN NEW; END IF;
  END IF;
  RAISE EXCEPTION 'subscription transition denied' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.subscription_guard() TO clinic_owner;

CREATE FUNCTION clinic_app.subscription_command(
 requested_clinic uuid, requested_action text, requested_plan text,
 expected_revision integer, command_key uuid)
RETURNS TABLE(subscription_id uuid, revision integer, state text, changed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE org uuid; sub clinic_app.subscriptions_subscription;
 receipt clinic_app.subscriptions_subscriptionevent; selected_plan uuid;
BEGIN
 org:=clinic_app.subscription_scope(requested_clinic,true);
 IF org IS NULL THEN RAISE EXCEPTION 'subscription denied' USING ERRCODE='42501';
  END IF;
 SELECT * INTO sub FROM clinic_app.subscriptions_subscription s
 WHERE s.organization_id=org FOR UPDATE;
 SELECT * INTO receipt FROM clinic_app.subscriptions_subscriptionevent e
 WHERE e.organization_id=org AND e.command_id=command_key;
 IF receipt.id IS NOT NULL THEN
  IF receipt.action=requested_action AND receipt.plan_code=requested_plan
   AND receipt.revision=expected_revision+1 THEN
   RETURN QUERY SELECT sub.id,receipt.revision,receipt.to_state::text,false;
  END IF;
  RETURN;
 END IF;
 IF sub.revision<>expected_revision OR sub.state IN ('cancelled','offboarded')
  OR requested_action NOT IN ('plan_changed','cancel') THEN RETURN; END IF;
 IF requested_action='plan_changed' THEN
  SELECT p.id INTO selected_plan FROM clinic_app.subscriptions_plan p
  WHERE p.code=requested_plan ORDER BY p.version DESC LIMIT 1;
  IF selected_plan IS NULL OR clinic_app.subscription_seats(org)>
   COALESCE((SELECT quota FROM clinic_app.subscriptions_entitlement
    WHERE plan_id=selected_plan AND meter='seats' AND enabled),0) THEN RETURN; END IF;
 ELSE
  IF requested_plan<>'' THEN RETURN; END IF;
  selected_plan:=sub.plan_id;
 END IF;
 UPDATE clinic_app.subscriptions_subscription s SET plan_id=selected_plan,
  state=CASE WHEN requested_action='cancel' THEN 'cancelled' ELSE sub.state END,
  revision=sub.revision+1,
  cancellation_at=CASE WHEN requested_action<>'cancel' THEN sub.cancellation_at
   WHEN sub.state<>'active' THEN clinic_app.subscription_clock()
   ELSE date_trunc('month',clinic_app.subscription_clock() AT TIME ZONE 'UTC')
    AT TIME ZONE 'UTC'+interval '1 month' END
 WHERE s.id=sub.id;
 INSERT INTO clinic_app.subscriptions_subscriptionevent
 (id,organization_id,subscription_id,command_id,action,plan_code,from_state,
  to_state,revision,occurred_at)
 SELECT gen_random_uuid(),org,s.id,command_key,requested_action,requested_plan,
  sub.state,s.state,s.revision,clinic_app.subscription_clock()
 FROM clinic_app.subscriptions_subscription s WHERE s.id=sub.id RETURNING * INTO
  receipt;
 RETURN QUERY SELECT sub.id,receipt.revision,receipt.to_state::text,true;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_command(uuid,text,text,integer,uuid)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.subscription_command(uuid,text,text,integer,
 uuid) TO clinic_app;

CREATE FUNCTION clinic_app.subscription_platform_event(
 requested_clinic uuid, requested_subscription uuid, event_key uuid,
  requested_action text)
RETURNS TABLE(subscription_id uuid, revision integer, state text, changed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE org uuid; sub clinic_app.subscriptions_subscription;
 receipt clinic_app.subscriptions_subscriptionevent; next_state text; deadline
  timestamptz;
BEGIN
 IF current_setting('role') NOT IN ('clinic_owner','none') THEN
  RAISE EXCEPTION 'subscription denied' USING ERRCODE='42501';
 END IF;
 org:=clinic_app.subscription_scope(requested_clinic,true);
 IF org IS NULL THEN RAISE EXCEPTION 'subscription denied' USING ERRCODE='42501';
  END IF;
 SELECT * INTO sub FROM clinic_app.subscriptions_subscription s
 WHERE s.id=requested_subscription AND s.organization_id=org FOR UPDATE;
 IF sub.id IS NULL THEN RETURN; END IF;
 SELECT * INTO receipt FROM clinic_app.subscriptions_subscriptionevent e
 WHERE e.organization_id=org AND e.command_id=event_key;
 IF receipt.id IS NOT NULL THEN
  IF receipt.subscription_id=sub.id AND receipt.action=requested_action AND
   receipt.plan_code='' THEN
   RETURN QUERY SELECT sub.id,receipt.revision,receipt.to_state::text,false;
  END IF;
  RETURN;
 END IF;
 next_state:=NULL; deadline:=sub.grace_ends_at;
 IF requested_action='paid' AND sub.state IN ('trial','active','past_due','grace',
  'restricted') THEN
  next_state:='active'; deadline:=NULL;
 ELSIF requested_action='payment_failed' AND sub.state='active' THEN
  next_state:='past_due'; deadline:=clinic_app.subscription_clock()+interval '7 days';
 ELSIF requested_action='advance' AND sub.state='past_due' THEN next_state:='grace';
 ELSIF requested_action='advance' AND sub.state='grace'
  AND clinic_app.subscription_clock()>=sub.grace_ends_at THEN next_state:='restricted';
 ELSIF requested_action='advance' AND sub.state='trial'
  AND clinic_app.subscription_clock()>=sub.trial_ends_at THEN next_state:='restricted';
 ELSIF requested_action='offboard' AND sub.state='cancelled'
  AND clinic_app.subscription_clock()>=sub.cancellation_at THEN
   next_state:='offboarded';
 END IF;
 IF next_state IS NULL THEN RETURN; END IF;
 UPDATE clinic_app.subscriptions_subscription s SET state=next_state,
  revision=sub.revision+1,grace_ends_at=deadline WHERE s.id=sub.id;
 INSERT INTO clinic_app.subscriptions_subscriptionevent
 (id,organization_id,subscription_id,command_id,action,plan_code,from_state,
  to_state,revision,occurred_at)
 VALUES (gen_random_uuid(),org,sub.id,event_key,requested_action,'',sub.state,
  next_state,
  sub.revision+1,clinic_app.subscription_clock()) RETURNING * INTO receipt;
 RETURN QUERY SELECT sub.id,receipt.revision,receipt.to_state::text,true;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.subscription_platform_event(uuid,uuid,uuid,text)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.subscription_platform_event(uuid,uuid,uuid,
 text) TO clinic_owner;
RESET ROLE;
"""

SQL += "\n".join(
    f"""
REVOKE ALL ON clinic_app.{table} FROM clinic_app;
GRANT SELECT ON clinic_app.{table} TO clinic_app;
ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY subscription_owner ON clinic_app.{table} TO clinic_owner
 USING (true) WITH CHECK (true);
CREATE POLICY subscription_read ON clinic_app.{table} FOR SELECT TO clinic_app
 USING ({
        "true"
        if table in TABLES[:2]
        else "organization_id=NULLIF("
        "current_setting('app.current_tenant',true),'')::uuid "
        "AND clinic_app.user_has_org(organization_id)"
    });
CREATE TRIGGER subscription_retained_row BEFORE UPDATE OR DELETE ON clinic_app.{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.subscription_guard();
CREATE TRIGGER subscription_retained_truncate BEFORE TRUNCATE ON clinic_app.{table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.subscription_guard();
"""
    for table in TABLES
)
SQL += """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.subscription_guard() FROM clinic_owner;
SET CONSTRAINTS ALL DEFERRED;
RESET ROLE;
"""

REVERSE = (
    """
DROP TRIGGER subscription_membership_created ON clinic_app.identity_userclinicrole;
DROP TRIGGER subscription_organization_created ON clinic_app.identity_organization;
"""
    + "\n".join(
        f"""
DROP TRIGGER subscription_retained_row ON clinic_app.{table};
DROP TRIGGER subscription_retained_truncate ON clinic_app.{table};
DROP POLICY subscription_read ON clinic_app.{table};
DROP POLICY subscription_owner ON clinic_app.{table};
"""
        for table in reversed(TABLES)
    )
    + """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.subscription_platform_event(uuid,uuid,uuid,text);
DROP FUNCTION clinic_app.subscription_command(uuid,text,text,integer,uuid);
DROP FUNCTION clinic_app.subscription_guard();
DROP FUNCTION clinic_app.subscription_identity_hook();
DROP FUNCTION clinic_app.subscription_bootstrap(uuid);
DROP FUNCTION clinic_app.subscription_seats(uuid);
DROP FUNCTION clinic_app.subscription_scope(uuid,boolean);
DROP FUNCTION clinic_app.subscription_clock();
RESET ROLE;
"""
)
