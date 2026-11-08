"""Version-one reporting authority and immutable projection generation."""

from apps.analytics.migrations._sources_v1_sql import SOURCES_SQL

TABLES = (
    "analytics_metricdefinitionversion",
    "analytics_readmodelrefresh",
    "analytics_savedreport",
    "analytics_analystreview",
)
# json.dumps of definitions_v1.METRICS' specifications, as a literal: the clock
# census decoder evaluates no runtime objects, and a migration keeps its snapshot.
# tests/analytics/test_migrations.py pins it to the frozen v1 definitions.
_catalog = (
    '{"utilization": {"schema": "metric-definition-v1", "metric_id": "utiliza'
    'tion", "version": 1, "grain": "clinic", "formula": {"op": "ratio", "nume'
    'rator": "numerator", "denominator": "denominator", "scale": 100}, "numer'
    'ator": "Booked, arrived, in-progress, completed and no-show practitioner'
    ' seconds", "denominator": "Unretired practitioner availability seconds; '
    'both clipped to the window", "unit": "percent", "time_basis": "window", '
    '"window": {"bounds": "[start,end)", "maximum_days": 366}, "timezone": "c'
    'linic", "filters": [], "dimensions": [], "late_data": "explicit refresh '
    'rebuilds; previous snapshots are retained", "freshness_seconds": 900, "a'
    'uthorization": {"all": ["charge.read"], "any": ["configuration.clinic", '
    '"configuration.organization"], "scope": "exact clinic; requester; curren'
    't authorization fingerprint"}, "quality": {"missing_source": "partial", '
    '"zero_denominator": "null", "unresolved": "partial", "forecast": "unvali'
    'dated; unavailable"}, "provenance": "scheduling.Appointment[start_at,end'
    '_at); AvailabilityBlock[start_at,end_at)", "available": true, "benchmark'
    'ing": "disabled; BLOCKED-ON-EG-2"}, "no_show": {"schema": "metric-defini'
    'tion-v1", "metric_id": "no_show", "version": 1, "grain": "clinic", "form'
    'ula": {"op": "ratio", "numerator": "numerator", "denominator": "denomina'
    'tor", "scale": 100}, "numerator": "Appointments resolved as no_show", "d'
    'enominator": "Appointments resolved as completed or no_show; unresolved '
    'visits are partial", "unit": "percent", "time_basis": "window", "window"'
    ': {"bounds": "[start,end)", "maximum_days": 366}, "timezone": "clinic", '
    '"filters": ["weekdays"], "dimensions": ["day"], "late_data": "explicit r'
    'efresh rebuilds; previous snapshots are retained", "freshness_seconds": '
    '900, "authorization": {"all": ["charge.read"], "any": ["configuration.cl'
    'inic", "configuration.organization"], "scope": "exact clinic; requester;'
    ' current authorization fingerprint"}, "quality": {"missing_source": "par'
    'tial", "zero_denominator": "null", "unresolved": "partial", "forecast": '
    '"unvalidated; unavailable"}, "provenance": "scheduling.Appointment.start'
    '_at", "available": true, "benchmarking": "disabled; BLOCKED-ON-EG-2"}, "'
    'waiting_time": {"schema": "metric-definition-v1", "metric_id": "waiting_'
    'time", "version": 1, "grain": "clinic", "formula": {"op": "ratio", "nume'
    'rator": "numerator", "denominator": "denominator", "scale": 1}, "numerat'
    'or": "Seconds from queue arrival to first call", "denominator": "Called '
    'queue entries; uncalled arrivals remain partial", "unit": "seconds", "ti'
    'me_basis": "window", "window": {"bounds": "[start,end)", "maximum_days":'
    ' 366}, "timezone": "clinic", "filters": ["weekdays"], "dimensions": ["da'
    'y"], "late_data": "explicit refresh rebuilds; previous snapshots are ret'
    'ained", "freshness_seconds": 900, "authorization": {"all": ["charge.read'
    '"], "any": ["configuration.clinic", "configuration.organization"], "scop'
    'e": "exact clinic; requester; current authorization fingerprint"}, "qual'
    'ity": {"missing_source": "partial", "zero_denominator": "null", "unresol'
    'ved": "partial", "forecast": "unvalidated; unavailable"}, "provenance": '
    '"scheduling.QueueEntry.created_at -> called_at", "available": true, "ben'
    'chmarking": "disabled; BLOCKED-ON-EG-2"}, "task_turnaround": {"schema": '
    '"metric-definition-v1", "metric_id": "task_turnaround", "version": 1, "g'
    'rain": "clinic", "formula": {"op": "ratio", "numerator": "numerator", "d'
    'enominator": "denominator", "scale": 1}, "numerator": "Seconds from task'
    ' creation to its terminal done update", "denominator": "Completed tasks;'
    ' unfinished tasks remain partial", "unit": "seconds", "time_basis": "win'
    'dow", "window": {"bounds": "[start,end)", "maximum_days": 366}, "timezon'
    'e": "clinic", "filters": ["weekdays"], "dimensions": ["day"], "late_data'
    '": "explicit refresh rebuilds; previous snapshots are retained", "freshn'
    'ess_seconds": 900, "authorization": {"all": ["charge.read"], "any": ["co'
    'nfiguration.clinic", "configuration.organization"], "scope": "exact clin'
    'ic; requester; current authorization fingerprint"}, "quality": {"missing'
    '_source": "partial", "zero_denominator": "null", "unresolved": "partial"'
    ', "forecast": "unvalidated; unavailable"}, "provenance": "workflows.Task'
    '.created_at -> updated_at(done)", "available": true, "benchmarking": "di'
    'sabled; BLOCKED-ON-EG-2"}, "receivables": {"schema": "metric-definition-'
    'v1", "metric_id": "receivables", "version": 1, "grain": "clinic", "formu'
    'la": {"op": "sum", "numerator": "numerator", "denominator": "one", "scal'
    'e": 1}, "numerator": "Posted patient_ar and insurer_ar debits less credi'
    'ts through the cutoff", "denominator": "One; cumulative BRL centavos, in'
    'cluding opening balances", "unit": "minor", "time_basis": "as_of", "wind'
    'ow": {"bounds": "[start,end)", "maximum_days": 366}, "timezone": "clinic'
    '", "filters": [], "dimensions": [], "late_data": "explicit refresh rebui'
    'lds; previous snapshots are retained", "freshness_seconds": 900, "author'
    'ization": {"all": ["charge.read"], "any": ["configuration.clinic", "conf'
    'iguration.organization"], "scope": "exact clinic; requester; current aut'
    'horization fingerprint"}, "quality": {"missing_source": "partial", "zero'
    '_denominator": "null", "unresolved": "partial", "forecast": "unvalidated'
    '; unavailable"}, "provenance": "billing.JournalBook -> JournalEntry.post'
    'ed_at -> JournalLine -> Account", "available": true, "benchmarking": "di'
    'sabled; BLOCKED-ON-EG-2"}, "verified_collections": {"schema": "metric-de'
    'finition-v1", "metric_id": "verified_collections", "version": 1, "grain"'
    ': "clinic", "formula": {"op": "sum", "numerator": "numerator", "denomina'
    'tor": "one", "scale": 1}, "numerator": "Posted collection asset debits; '
    'unverified observations are excluded", "denominator": "One; BRL centavos'
    ' before fees and refunds", "unit": "minor", "time_basis": "window", "win'
    'dow": {"bounds": "[start,end)", "maximum_days": 366}, "timezone": "clini'
    'c", "filters": ["weekdays"], "dimensions": ["day"], "late_data": "explic'
    'it refresh rebuilds; previous snapshots are retained", "freshness_second'
    's": 900, "authorization": {"all": ["charge.read"], "any": ["configuratio'
    'n.clinic", "configuration.organization"], "scope": "exact clinic; reques'
    'ter; current authorization fingerprint"}, "quality": {"missing_source": '
    '"partial", "zero_denominator": "null", "unresolved": "partial", "forecas'
    't": "unvalidated; unavailable"}, "provenance": "billing.JournalEntry(col'
    'lection).posted_at -> JournalLine -> Account", "available": true, "bench'
    'marking": "disabled; BLOCKED-ON-EG-2"}, "net_collections": {"schema": "m'
    'etric-definition-v1", "metric_id": "net_collections", "version": 1, "gra'
    'in": "clinic", "formula": {"op": "sum", "numerator": "numerator", "denom'
    'inator": "one", "scale": 1}, "numerator": "Posted collections less confi'
    'rmed refund and chargeback asset credits", "denominator": "One; BRL cent'
    'avos before PSP fees", "unit": "minor", "time_basis": "window", "window"'
    ': {"bounds": "[start,end)", "maximum_days": 366}, "timezone": "clinic", '
    '"filters": ["weekdays"], "dimensions": ["day"], "late_data": "explicit r'
    'efresh rebuilds; previous snapshots are retained", "freshness_seconds": '
    '900, "authorization": {"all": ["charge.read"], "any": ["configuration.cl'
    'inic", "configuration.organization"], "scope": "exact clinic; requester;'
    ' current authorization fingerprint"}, "quality": {"missing_source": "par'
    'tial", "zero_denominator": "null", "unresolved": "partial", "forecast": '
    '"unvalidated; unavailable"}, "provenance": "billing.JournalEntry(collect'
    'ion,refund_confirmed).posted_at -> JournalLine", "available": true, "ben'
    'chmarking": "disabled; BLOCKED-ON-EG-2"}, "denials": {"schema": "metric-'
    'definition-v1", "metric_id": "denials", "version": 1, "grain": "clinic",'
    ' "formula": {"op": "sum", "numerator": "numerator", "denominator": "one"'
    ', "scale": 1}, "numerator": "Adjudicated denial postings; generic payer '
    'adjustments are not denials", "denominator": "One; no reviewed denial po'
    'sting classification exists on this base", "unit": "minor", "time_basis"'
    ': "window", "window": {"bounds": "[start,end)", "maximum_days": 366}, "t'
    'imezone": "clinic", "filters": ["weekdays"], "dimensions": ["day"], "lat'
    'e_data": "explicit refresh rebuilds; previous snapshots are retained", "'
    'freshness_seconds": 900, "authorization": {"all": ["charge.read"], "any"'
    ': ["configuration.clinic", "configuration.organization"], "scope": "exac'
    't clinic; requester; current authorization fingerprint"}, "quality": {"m'
    'issing_source": "partial", "zero_denominator": "null", "unresolved": "pa'
    'rtial", "forecast": "unvalidated; unavailable"}, "provenance": "Journal-'
    'only source required; insurance denial posting contract unavailable", "a'
    'vailable": false, "benchmarking": "disabled; BLOCKED-ON-EG-2"}, "payout_'
    'liabilities": {"schema": "metric-definition-v1", "metric_id": "payout_li'
    'abilities", "version": 1, "grain": "clinic", "formula": {"op": "sum", "n'
    'umerator": "numerator", "denominator": "one", "scale": 1}, "numerator": '
    '"Posted practitioner_payables and payout_in_transit credits less debits"'
    ', "denominator": "One; cumulative BRL centavos, including retained unkno'
    'wn outcomes", "unit": "minor", "time_basis": "as_of", "window": {"bounds'
    '": "[start,end)", "maximum_days": 366}, "timezone": "clinic", "filters":'
    ' [], "dimensions": [], "late_data": "explicit refresh rebuilds; previous'
    ' snapshots are retained", "freshness_seconds": 900, "authorization": {"a'
    'll": ["charge.read"], "any": ["configuration.clinic", "configuration.org'
    'anization"], "scope": "exact clinic; requester; current authorization fi'
    'ngerprint"}, "quality": {"missing_source": "partial", "zero_denominator"'
    ': "null", "unresolved": "partial", "forecast": "unvalidated; unavailable'
    '"}, "provenance": "billing.JournalEntry.posted_at -> JournalLine -> Acco'
    'unt", "available": true, "benchmarking": "disabled; BLOCKED-ON-EG-2"}, "'
    'result_ack_latency": {"schema": "metric-definition-v1", "metric_id": "re'
    'sult_ack_latency", "version": 1, "grain": "clinic", "formula": {"op": "r'
    'atio", "numerator": "numerator", "denominator": "denominator", "scale": '
    '1}, "numerator": "Seconds from matched result version arrival to its ack'
    'nowledgment", "denominator": "Acknowledged matched versions; unacknowled'
    'ged versions remain partial", "unit": "seconds", "time_basis": "window",'
    ' "window": {"bounds": "[start,end)", "maximum_days": 366}, "timezone": "'
    'clinic", "filters": ["weekdays"], "dimensions": ["day"], "late_data": "e'
    'xplicit refresh rebuilds; previous snapshots are retained", "freshness_s'
    'econds": 900, "authorization": {"all": ["charge.read"], "any": ["configu'
    'ration.clinic", "configuration.organization"], "scope": "exact clinic; r'
    'equester; current authorization fingerprint"}, "quality": {"missing_sour'
    'ce": "partial", "zero_denominator": "null", "unresolved": "partial", "fo'
    'recast": "unvalidated; unavailable"}, "provenance": "orders.ResultVersio'
    'n.created_at -> ResultAcknowledgment.created_at", "available": true, "be'
    'nchmarking": "disabled; BLOCKED-ON-EG-2"}, "follow_up_overdue": {"schema'
    '": "metric-definition-v1", "metric_id": "follow_up_overdue", "version": '
    '1, "grain": "clinic", "formula": {"op": "sum", "numerator": "numerator",'
    ' "denominator": "one", "scale": 1}, "numerator": "Nonterminal follow_up '
    'tasks with a deadline before refresh time", "denominator": "One; tasks w'
    'hose due date lies in the window", "unit": "count", "time_basis": "windo'
    'w", "window": {"bounds": "[start,end)", "maximum_days": 366}, "timezone"'
    ': "clinic", "filters": ["weekdays"], "dimensions": ["day"], "late_data":'
    ' "explicit refresh rebuilds; previous snapshots are retained", "freshnes'
    's_seconds": 900, "authorization": {"all": ["charge.read"], "any": ["conf'
    'iguration.clinic", "configuration.organization"], "scope": "exact clinic'
    '; requester; current authorization fingerprint"}, "quality": {"missing_s'
    'ource": "partial", "zero_denominator": "null", "unresolved": "partial", '
    '"forecast": "unvalidated; unavailable"}, "provenance": "workflows.Task.d'
    'ue_at", "available": true, "benchmarking": "disabled; BLOCKED-ON-EG-2"},'
    ' "support_load": {"schema": "metric-definition-v1", "metric_id": "suppor'
    't_load", "version": 1, "grain": "clinic", "formula": {"op": "sum", "nume'
    'rator": "numerator", "denominator": "one", "scale": 1}, "numerator": "Op'
    'en customer support cases created in the window", "denominator": "One; n'
    'o support-case source is installed on this base", "unit": "count", "time'
    '_basis": "window", "window": {"bounds": "[start,end)", "maximum_days": 3'
    '66}, "timezone": "clinic", "filters": ["weekdays"], "dimensions": ["day"'
    '], "late_data": "explicit refresh rebuilds; previous snapshots are retai'
    'ned", "freshness_seconds": 900, "authorization": {"all": ["charge.read"]'
    ', "any": ["configuration.clinic", "configuration.organization"], "scope"'
    ': "exact clinic; requester; current authorization fingerprint"}, "qualit'
    'y": {"missing_source": "partial", "zero_denominator": "null", "unresolve'
    'd": "partial", "forecast": "unvalidated; unavailable"}, "provenance": "U'
    'navailable support-case source; operational tasks are not support cases"'
    ', "available": false, "benchmarking": "disabled; BLOCKED-ON-EG-2"}, "ai_'
    'correction_time": {"schema": "metric-definition-v1", "metric_id": "ai_co'
    'rrection_time", "version": 1, "grain": "clinic", "formula": {"op": "rati'
    'o", "numerator": "numerator", "denominator": "denominator", "scale": 1},'
    ' "numerator": "Elapsed seconds from suggestion creation to a modified re'
    'view", "denominator": "Modified suggestions; includes queue time, not ac'
    'tive editing time", "unit": "seconds", "time_basis": "window", "window":'
    ' {"bounds": "[start,end)", "maximum_days": 366}, "timezone": "clinic", "'
    'filters": ["weekdays"], "dimensions": ["day"], "late_data": "explicit re'
    'fresh rebuilds; previous snapshots are retained", "freshness_seconds": 9'
    '00, "authorization": {"all": ["charge.read"], "any": ["configuration.cli'
    'nic", "configuration.organization"], "scope": "exact clinic; requester; '
    'current authorization fingerprint"}, "quality": {"missing_source": "part'
    'ial", "zero_denominator": "null", "unresolved": "partial", "forecast": "'
    'unvalidated; unavailable"}, "provenance": "scribe.ReviewDecision.created'
    '_at; duration from DraftSectionSuggestion.created_at", "available": true'
    ', "benchmarking": "disabled; BLOCKED-ON-EG-2"}, "activation": {"schema":'
    ' "metric-definition-v1", "metric_id": "activation", "version": 1, "grain'
    '": "clinic", "formula": {"op": "ratio", "numerator": "numerator", "denom'
    'inator": "denominator", "scale": 100}, "numerator": "Current exact-clini'
    'c staff with an operational audit event in the window", "denominator": "'
    'Current active exact-clinic staff; excludes reporting events", "unit": "'
    'percent", "time_basis": "window", "window": {"bounds": "[start,end)", "m'
    'aximum_days": 366}, "timezone": "clinic", "filters": [], "dimensions": ['
    '], "late_data": "explicit refresh rebuilds; previous snapshots are retai'
    'ned", "freshness_seconds": 900, "authorization": {"all": ["charge.read"]'
    ', "any": ["configuration.clinic", "configuration.organization"], "scope"'
    ': "exact clinic; requester; current authorization fingerprint"}, "qualit'
    'y": {"missing_source": "partial", "zero_denominator": "null", "unresolve'
    'd": "partial", "forecast": "unvalidated; unavailable"}, "provenance": "i'
    'dentity.UserClinicRole; audit.AuditEvent metadata only", "available": tr'
    'ue, "benchmarking": "disabled; BLOCKED-ON-EG-2"}, "retained_usage": {"sc'
    'hema": "metric-definition-v1", "metric_id": "retained_usage", "version":'
    ' 1, "grain": "clinic", "formula": {"op": "ratio", "numerator": "numerato'
    'r", "denominator": "denominator", "scale": 100}, "numerator": "Paid staf'
    'f active in both adjacent equal windows", "denominator": "Paid staff act'
    'ive in the preceding window; subscription evidence required", "unit": "p'
    'ercent", "time_basis": "window", "window": {"bounds": "[start,end)", "ma'
    'ximum_days": 366}, "timezone": "clinic", "filters": [], "dimensions": []'
    ', "late_data": "explicit refresh rebuilds; previous snapshots are retain'
    'ed", "freshness_seconds": 900, "authorization": {"all": ["charge.read"],'
    ' "any": ["configuration.clinic", "configuration.organization"], "scope":'
    ' "exact clinic; requester; current authorization fingerprint"}, "quality'
    '": {"missing_source": "partial", "zero_denominator": "null", "unresolved'
    '": "partial", "forecast": "unvalidated; unavailable"}, "provenance": "Su'
    "bscription and metered usage source unavailable; never inferred from the"
    ' clinic journal", "available": false, "benchmarking": "disabled; BLOCKED'
    '-ON-EG-2"}}'
)
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

CREATE FUNCTION clinic_app.analytics_validate(requested_clinic uuid, query jsonb,
 window_start timestamptz, window_end timestamptz)
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
 OR window_start IS NULL OR window_end IS NULL
 OR query->'window'->>'start'<>to_char(window_start AT TIME ZONE 'UTC',
 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
 OR query->'window'->>'end'<>to_char(window_end AT TIME ZONE 'UTC',
 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
 THEN RAISE EXCEPTION 'invalid metric query' USING ERRCODE='22023'; END IF;
 first_at:=window_start;
 last_at:=window_end;
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
 command uuid, window_start timestamptz, window_end timestamptz)
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
 PERFORM clinic_app.analytics_validate(requested_clinic,query,window_start,window_end);
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
 first_at:=window_start;
 last_at:=window_end;
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
 descriptor,window_start,window_end,command_id,rows,quality,as_of)
 VALUES(refresh_id,tenant,requested_clinic,actor,definition,auth,query,window_start,
 window_end,command,rows,state,observed);
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
 PERFORM clinic_app.analytics_validate(NEW.clinic_id,NEW.descriptor,
 NEW.window_start,NEW.window_end);
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
    ("analytics_validate(uuid,jsonb,timestamptz,timestamptz)", False),
    ("analytics_refresh(uuid,jsonb,uuid,timestamptz,timestamptz)", True),
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
 clinic_app.analytics_refresh(uuid,jsonb,uuid,timestamptz,timestamptz),
 clinic_app.analytics_validate(uuid,jsonb,timestamptz,timestamptz),
 clinic_app.analytics_authorization(uuid),
 clinic_app.analytics_allowed(uuid,text);
RESET ROLE;
REVOKE SELECT (organization_id,actor_user_id,event_type,occurred_at_utc,payload)
 ON clinic_app.audit_event_tenant FROM clinic_resolver;
"""
