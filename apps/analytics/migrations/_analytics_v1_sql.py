"""Version-one reporting authority and immutable projection generation."""

import json

from apps.analytics.definitions_v1 import METRICS
from apps.analytics.migrations._sources_v1_sql import SOURCES_SQL

TABLES = (
    "analytics_metricdefinitionversion",
    "analytics_readmodelrefresh",
    "analytics_savedreport",
    "analytics_analystreview",
)
_catalog = json.dumps({key: value.specification() for key, value in METRICS.items()})
SQL = """
REVOKE ALL ON clinic_app.analytics_metricdefinitionversion,
 clinic_app.analytics_readmodelrefresh,clinic_app.analytics_savedreport,
 clinic_app.analytics_analystreview FROM PUBLIC,clinic_app,clinic_agent,clinic_resolver;
GRANT SELECT ON clinic_app.analytics_metricdefinitionversion,
 clinic_app.analytics_readmodelrefresh,clinic_app.analytics_savedreport,
 clinic_app.analytics_analystreview TO clinic_app,clinic_resolver;
GRANT INSERT ON clinic_app.analytics_metricdefinitionversion,
 clinic_app.analytics_readmodelrefresh TO clinic_resolver;
GRANT INSERT ON clinic_app.analytics_savedreport,
 clinic_app.analytics_analystreview TO clinic_app;
GRANT SELECT (organization_id,actor_user_id,event_type,occurred_at_utc,payload)
 ON clinic_app.audit_event_tenant TO clinic_resolver;
GRANT SELECT ON clinic_app.scribe_reviewdecision,
 clinic_app.scribe_draftsectionsuggestion,
 clinic_app.scribe_aidraft TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;

CREATE FUNCTION clinic_app.analytics_allowed(requested_clinic uuid, metric text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT metric=ANY(ARRAY['utilization','no_show','waiting_time','task_turnaround',
 'receivables','verified_collections','net_collections','denials','payout_liabilities',
 'result_ack_latency','follow_up_overdue','support_load','ai_correction_time',
 'activation','retained_usage'])
 AND NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND NULLIF(current_setting('app.current_principal',true),'') IS NULL
 AND EXISTS (SELECT 1 FROM clinic_app.identity_clinic c WHERE c.id=requested_clinic
 AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 AND clinic_app.has_permission('charge.read',requested_clinic,NULL)
 AND (clinic_app.has_permission('configuration.clinic',requested_clinic,NULL)
 OR clinic_app.has_permission('configuration.organization',requested_clinic,NULL))
$f$;

CREATE FUNCTION clinic_app.analytics_authorization(requested_clinic uuid)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 result text;
BEGIN
 IF NOT coalesce(clinic_app.analytics_allowed(requested_clinic,'no_show'),false) THEN
  RAISE EXCEPTION 'metric access denied' USING ERRCODE='42501';
 END IF;
 SELECT encode(clinic_app.digest('analytics-auth-v1:'||actor::text||':'||
 requested_clinic::text||':'||
 coalesce((SELECT string_agg(
 r.id::text||':'||r.role||':'||r.workflow_bundle_version::text,
 ',' ORDER BY r.id) FROM clinic_app.identity_userclinicrole r
 WHERE r.clinic_id=requested_clinic AND r.user_id=actor AND r.revoked_at IS NULL),
 '')||':'||
 coalesce((SELECT string_agg(g.id::text,',' ORDER BY g.id)
 FROM clinic_app.identity_rolegrant g WHERE g.clinic_id=requested_clinic
 AND g.valid_from<=statement_timestamp()
 AND (g.valid_to IS NULL OR g.valid_to>statement_timestamp())),''),
 'sha256'),'hex') INTO result;
 RETURN result;
END $f$;

CREATE FUNCTION clinic_app.analytics_validate(requested_clinic uuid, query jsonb)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE first_at timestamptz; last_at timestamptz; day jsonb; days jsonb;
BEGIN
 IF NOT coalesce(clinic_app.analytics_allowed(requested_clinic,query->>'metric_id'),
 false)
 THEN RAISE EXCEPTION 'metric access denied' USING ERRCODE='42501'; END IF;
 IF jsonb_typeof(query)<>'object' OR NOT query ?& ARRAY[
 'metric_id','version','dimensions','filters','window','tz']
 OR query-ARRAY['metric_id','version','dimensions','filters','window','tz']<>'{}'
 OR query->'version'<>'1'::jsonb
 OR query->'dimensions' NOT IN ('[]'::jsonb,'["day"]'::jsonb)
 OR jsonb_typeof(query->'filters')<>'object'
 OR (query->'filters')-'weekdays'<>'{}'
 OR jsonb_typeof(query->'window')<>'object'
 OR NOT (query->'window') ?& ARRAY['start','end']
 OR (query->'window')-ARRAY['start','end']<>'{}'
 OR jsonb_typeof(query->'tz')<>'string'
 OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
 WHERE c.id=requested_clinic AND c.timezone=query->>'tz')
 OR jsonb_typeof(query->'window'->'start')<>'string'
 OR jsonb_typeof(query->'window'->'end')<>'string'
 OR query->'window'->>'start' !~ (
 '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
 '([.][0-9]{1,6})?(Z|[+-](0[0-9]|1[0-4]):[0-5][0-9])$')
 OR query->'window'->>'end' !~ (
 '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
 '([.][0-9]{1,6})?(Z|[+-](0[0-9]|1[0-4]):[0-5][0-9])$')
 THEN RAISE EXCEPTION 'invalid metric query' USING ERRCODE='22023'; END IF;
 first_at:=(query->'window'->>'start')::timestamptz;
 last_at:=(query->'window'->>'end')::timestamptz;
 IF NOT isfinite(first_at) OR NOT isfinite(last_at) OR last_at<=first_at
 OR last_at-first_at>interval '366 days' THEN
 RAISE EXCEPTION 'invalid metric query' USING ERRCODE='22023'; END IF;
 days:=coalesce(query->'filters'->'weekdays','[]'::jsonb);
 IF jsonb_typeof(days)<>'array' OR jsonb_array_length(days)>7
 OR (SELECT count(DISTINCT value) FROM jsonb_array_elements(days))<>jsonb_array_length(
 days)
 OR (query->>'metric_id' IN ('utilization','receivables','payout_liabilities',
 'activation','retained_usage')
 AND (query->'dimensions'<>'[]' OR days<>'[]')) THEN
 RAISE EXCEPTION 'invalid metric query' USING ERRCODE='22023'; END IF;
 FOR day IN SELECT value FROM jsonb_array_elements(days) LOOP
  IF jsonb_typeof(day)<>'number' OR day::text !~ '^[0-6]$' THEN
  RAISE EXCEPTION 'invalid metric query' USING ERRCODE='22023'; END IF;
 END LOOP;
END $f$;
"""
SQL += SOURCES_SQL
SQL += """
CREATE FUNCTION clinic_app.analytics_refresh(requested_clinic uuid, query jsonb,
 command uuid)
RETURNS TABLE(refresh_id uuid, changed boolean, registered boolean)
LANGUAGE plpgsql VOLATILE SECURITY
 DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 tenant uuid:=NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 auth text; previous clinic_app.analytics_readmodelrefresh; definition uuid;
 first_at timestamptz; last_at timestamptz; observed timestamptz:=statement_timestamp();
 rows jsonb; top numeric; bottom numeric; missing bigint; state text; catalog jsonb;
BEGIN
 PERFORM clinic_app.analytics_validate(requested_clinic,query);
 IF command IS NULL THEN RAISE EXCEPTION 'invalid metric query' USING ERRCODE='22023';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('clinic-audit:'||tenant::text,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('analytics:'||requested_clinic::text,
 0));
 auth:=clinic_app.analytics_authorization(requested_clinic);
 SELECT * INTO previous FROM clinic_app.analytics_readmodelrefresh
 WHERE clinic_id=requested_clinic AND user_id=actor AND command_id=command;
 IF FOUND THEN
  IF previous.descriptor<>query OR previous.authorization_version<>auth THEN
   RAISE EXCEPTION 'metric command conflicts' USING ERRCODE='23514';
  END IF;
  RETURN QUERY SELECT previous.id,false,false; RETURN;
 END IF;
 catalog:=__CATALOG__::jsonb;
 SELECT id INTO definition FROM clinic_app.analytics_metricdefinitionversion
 WHERE clinic_id=requested_clinic AND metric_id=query->>'metric_id' AND version=1;
 registered:=NOT FOUND;
 IF registered THEN
  definition:=gen_random_uuid();
  INSERT INTO clinic_app.analytics_metricdefinitionversion
  (id,organization_id,clinic_id,metric_id,version,specification,created_at)
  VALUES(definition,tenant,requested_clinic,query->>'metric_id',1,
  catalog->(query->>'metric_id'),observed);
 END IF;
 first_at:=(query->'window'->>'start')::timestamptz;
 last_at:=(query->'window'->>'end')::timestamptz;
 state:='complete';
 IF query->>'metric_id' IN ('denials','support_load','retained_usage') THEN
  rows:='[{"dimension":"","numerator":"0","denominator":"0","missing":"1"}]';
  state:='partial';
 ELSIF query->>'metric_id'='activation' THEN
  WITH staff AS (
   SELECT user_id, bool_or(occurred_at>=first_at AND occurred_at<last_at) AS active
   FROM clinic_app.analytics_usagefact WHERE clinic_id=requested_clinic GROUP BY user_id
  ) SELECT count(*) FILTER (WHERE active), count(*)
  INTO top,bottom FROM staff;
  rows:=jsonb_build_array(jsonb_build_object('dimension','','numerator',top::text,
   'denominator',bottom::text,'missing','0'));
 ELSE
  WITH facts AS (
   SELECT CASE WHEN query->'dimensions'='["day"]' THEN
    to_char(f.occurred_at AT TIME ZONE (query->>'tz'),'YYYY-MM-DD') ELSE '' END AS dim,
   CASE WHEN f.metric_id='utilization' AND f.numerator<>0 THEN
    extract(epoch FROM least(f.ended_at,last_at)-greatest(f.occurred_at,first_at))
    ELSE f.numerator END AS n,
   CASE WHEN f.metric_id='utilization' AND f.denominator<>0 THEN
    extract(epoch FROM least(f.ended_at,last_at)-greatest(f.occurred_at,first_at))
    ELSE f.denominator END AS d,f.missing
   FROM clinic_app.analytics_metricfact f
   WHERE f.clinic_id=requested_clinic AND f.metric_id=query->>'metric_id'
   AND f.occurred_at<last_at
   AND (f.metric_id IN ('receivables','payout_liabilities')
    OR f.occurred_at>=first_at OR (f.metric_id='utilization' AND f.ended_at>first_at))
   AND (f.metric_id<>'follow_up_overdue' OR f.occurred_at<observed)
   AND (coalesce(query->'filters'->'weekdays','[]')='[]'
    OR (query->'filters'->'weekdays') @> to_jsonb(
    (extract(isodow FROM f.occurred_at AT TIME ZONE (query->>'tz'))::int+6)%7))
  ), totals AS (
   SELECT facts.dim,sum(facts.n) AS n,sum(facts.d) AS d,
   sum(facts.missing) AS m FROM facts GROUP BY facts.dim
  )
  SELECT jsonb_agg(jsonb_build_object('dimension',dim,'numerator',n::text,
   'denominator',CASE WHEN query->>'metric_id' IN
   ('receivables','verified_collections','net_collections','payout_liabilities',
   'follow_up_overdue') THEN '1' ELSE d::text END,
   'missing',m::text) ORDER BY dim),coalesce(sum(m),0)
  INTO rows,missing FROM totals;
  rows:=coalesce(rows,
 '[{"dimension":"","numerator":"0","denominator":"0","missing":"0"}]');
  IF missing>0 THEN state:='partial'; END IF;
  IF query->>'metric_id' IN ('receivables','verified_collections','net_collections',
 'payout_liabilities')
  AND NOT EXISTS(SELECT 1 FROM clinic_app.billing_journalbook WHERE
 clinic_id=requested_clinic)
  THEN state:='unavailable'; END IF;
 END IF;
 refresh_id:=gen_random_uuid(); changed:=true;
 INSERT INTO clinic_app.analytics_readmodelrefresh
 (id,organization_id,clinic_id,user_id,definition_id,authorization_version,
 descriptor,command_id,rows,quality,as_of)
 VALUES(refresh_id,tenant,requested_clinic,actor,definition,auth,query,command,rows,
 state,observed);
 RETURN NEXT;
END $f$;

CREATE FUNCTION clinic_app.analytics_guard()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_OP<>'INSERT' THEN
  RAISE EXCEPTION 'metric history is immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.organization_id IS DISTINCT FROM
 NULLIF(current_setting('app.current_tenant',true),'')::uuid
 OR NEW.user_id IS DISTINCT FROM NULLIF(current_setting('app.current_user_id',true),
 '')::uuid
 THEN RAISE EXCEPTION 'metric access denied' USING ERRCODE='42501'; END IF;
 PERFORM clinic_app.analytics_validate(NEW.clinic_id,NEW.descriptor);
 IF TG_TABLE_NAME='analytics_savedreport' THEN
  IF NOT EXISTS(SELECT 1 FROM clinic_app.analytics_metricdefinitionversion d
  WHERE d.id=NEW.definition_id AND d.clinic_id=NEW.clinic_id
  AND d.organization_id=NEW.organization_id AND d.metric_id=NEW.descriptor->>'metric_id'
  AND d.version=1) THEN
   RAISE EXCEPTION 'metric access denied' USING ERRCODE='42501';
  END IF;
 ELSE
  IF NEW.decision NOT IN ('accepted','rejected')
  OR NEW.authorization_version<>clinic_app.analytics_authorization(NEW.clinic_id)
  OR NOT EXISTS(SELECT 1 FROM clinic_app.ai_aiinvocation i
   WHERE i.id=NEW.invocation_id AND i.clinic_id=NEW.clinic_id
   AND i.organization_id=NEW.organization_id AND i.user_id=NEW.user_id
   AND i.purpose='analyst' AND i.status='succeeded') THEN
   RAISE EXCEPTION 'metric access denied' USING ERRCODE='42501';
  END IF;
 END IF;
 RETURN NEW;
END $f$;
RESET ROLE;
"""
SQL = SQL.replace("__CATALOG__", "'" + _catalog.replace("'", "''") + "'")
for _table in TABLES:
    _access = (
        "analytics_allowed(clinic_id,metric_id)"
        if _table == "analytics_metricdefinitionversion"
        else "analytics_allowed(clinic_id,descriptor->>'metric_id') AND "
        "user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid"
    )
    if _table == "analytics_readmodelrefresh":
        _access += (
            " AND authorization_version=clinic_app.analytics_authorization(clinic_id)"
        )
    SQL += f"""
ALTER TABLE clinic_app.{_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{_table} FORCE ROW LEVEL SECURITY;
CREATE POLICY analytics_scope ON clinic_app.{_table} TO clinic_app USING (
 organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.{_access});
CREATE TRIGGER analytics_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
 ON clinic_app.{_table} FOR EACH STATEMENT
 EXECUTE FUNCTION clinic_app.analytics_guard();
"""
for _table in ("analytics_savedreport", "analytics_analystreview"):
    SQL += f"""
CREATE TRIGGER analytics_insert BEFORE INSERT ON clinic_app.{_table}
FOR EACH ROW EXECUTE FUNCTION clinic_app.analytics_guard();
"""
SQL += "SET LOCAL ROLE clinic_resolver;\n"
for _signature, _public in (
    ("analytics_allowed(uuid,text)", True),
    ("analytics_authorization(uuid)", True),
    ("analytics_validate(uuid,jsonb)", False),
    ("analytics_refresh(uuid,jsonb,uuid)", True),
    ("analytics_guard()", False),
):
    SQL += (
        f"REVOKE ALL ON FUNCTION clinic_app.{_signature} "
        "FROM PUBLIC,clinic_app,clinic_agent;\n"
    )
    if _public:
        SQL += f"GRANT EXECUTE ON FUNCTION clinic_app.{_signature} TO clinic_app;\n"
SQL += """
REVOKE ALL ON clinic_app.analytics_metricfact,clinic_app.analytics_usagefact
 FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT ON clinic_app.analytics_metricfact TO clinic_app;
RESET ROLE;
"""

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DO $f$
BEGIN
 IF EXISTS(SELECT 1 FROM clinic_app.analytics_metricdefinitionversion)
 OR EXISTS(SELECT 1 FROM clinic_app.analytics_readmodelrefresh)
 OR EXISTS(SELECT 1 FROM clinic_app.analytics_savedreport)
 OR EXISTS(SELECT 1 FROM clinic_app.analytics_analystreview) THEN
  RAISE EXCEPTION 'metric history requires restore' USING ERRCODE='55000';
 END IF;
END $f$;
DROP VIEW clinic_app.analytics_metricfact,clinic_app.analytics_usagefact;
RESET ROLE;
"""
for _table in TABLES:
    REVERSE_SQL += f"""
DROP TRIGGER analytics_immutable ON clinic_app.{_table};
DROP POLICY analytics_scope ON clinic_app.{_table};
"""
for _table in ("analytics_savedreport", "analytics_analystreview"):
    REVERSE_SQL += f"DROP TRIGGER analytics_insert ON clinic_app.{_table};\n"
REVERSE_SQL += """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.analytics_guard(),
 clinic_app.analytics_refresh(uuid,jsonb,uuid),
 clinic_app.analytics_validate(uuid,jsonb),
 clinic_app.analytics_authorization(uuid),
 clinic_app.analytics_allowed(uuid,text);
RESET ROLE;
REVOKE SELECT (organization_id,actor_user_id,event_type,occurred_at_utc,payload)
 ON clinic_app.audit_event_tenant FROM clinic_resolver;
"""
