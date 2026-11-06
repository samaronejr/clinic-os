"""Release notices and urgent concerns: exact posture, guards and bridges.

Additive. No table holds patient text: a notice is clinic-wide approved wording,
a delivery is provenance next to the comms message it queued, a concern points at
the patient's own portal message. Two bridges write without a staff actor. The
patient bridge reports a concern only for a message that session wrote in the same
transaction. The outcome bridge runs when a release notice's outbox operation
fails or is cancelled: it marks the delivery failed once and opens one
source-backed follow-up task for reception, which the task guard admits exactly.
"""

from typing import Final

from apps.workflows.migrations._care_sql import TASK_GUARD as CARE_TASK_GUARD

_SCOPE = "organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid"
_ACTOR = "NULLIF(current_setting('app.current_user_id',true),'')::uuid"
_TENANT = "NULLIF(current_setting('app.current_tenant',true),'')::uuid"
TABLES: Final = (
    "workflows_releasenoticetemplate",
    "workflows_releasedelivery",
    "workflows_urgentcoveragepolicy",
    "workflows_urgentconcern",
)
_ALL = ",".join(f"clinic_app.{table}" for table in TABLES)
RESPONSIBLE: Final = "'nurse','physician','receptionist','clinic_manager'"
FOLLOW_UP_HOURS: Final = 4

# The task guard admits the release follow-up as a source-backed system task,
# next to the orders, retention and care ones, derived from the deployed text.
_CARE_RETURN = (
    " IF TG_TABLE_NAME='workflows_task' AND TG_OP='INSERT'\n"
    " AND clinic_app.workflows_care_system_task_valid(to_jsonb(NEW))"
    " THEN RETURN NEW; END IF;\n"
)
_RELEASE_RETURN = (
    " IF TG_TABLE_NAME='workflows_task' AND TG_OP='INSERT'\n"
    " AND clinic_app.workflows_release_system_task_valid(to_jsonb(NEW))"
    " THEN RETURN NEW; END IF;\n"
)
TASK_GUARD: Final = CARE_TASK_GUARD.replace(
    _CARE_RETURN, _CARE_RETURN + _RELEASE_RETURN, 1
)
if len(TASK_GUARD.split(_RELEASE_RETURN)) - 1 != 1:
    msg = "the deployed task guard changed shape"
    raise RuntimeError(msg)

# The follow-up row the outcome bridge writes and the task guard admits.
_FOLLOW_UP_ROW = """jsonb_build_object(
  'organization_id',d.organization_id,'clinic_id',d.clinic_id,'kind','follow_up',
  'subject_ref',jsonb_build_object('kind','enrollment','id',d.enrollment_id::text),
  'created_by_id',NULL,'owner_user_id',NULL,'owner_role','receptionist',
  'priority','high','state','assigned','escalation_policy_version',1,
  'escalated_at',NULL,'depends_on_id',NULL,'completion_evidence','{}'::jsonb,
  'revision',1,'idempotency_key',d.id,'fingerprint',encode(clinic_app.digest(
   'workflows-release-failure:'||d.id::text,'sha256'),'hex'),
  'last_command_key',NULL,'last_command_digest','')"""

SQL = (
    """
REVOKE ALL ON __ALL__ FROM PUBLIC, clinic_app, clinic_agent;
GRANT SELECT,INSERT ON clinic_app.workflows_releasenoticetemplate,
 clinic_app.workflows_releasedelivery,clinic_app.workflows_urgentcoveragepolicy
 TO clinic_app;
GRANT SELECT ON clinic_app.workflows_urgentconcern TO clinic_app;
GRANT UPDATE (state,acknowledged_by_id,acknowledged_at,resolved_by_id,resolved_at,
 revision) ON clinic_app.workflows_urgentconcern TO clinic_app;
GRANT SELECT ON clinic_app.workflows_urgentcoveragepolicy,
 clinic_app.workflows_urgentconcern,clinic_app.workflows_releasedelivery,
 clinic_app.comms_message,clinic_app.comms_conversation,
 clinic_app.identity_userclinicrole TO clinic_resolver;
GRANT INSERT ON clinic_app.workflows_urgentconcern TO clinic_resolver;
GRANT UPDATE (failure_reason,failure_task_id)
 ON clinic_app.workflows_releasedelivery TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
-- The current user holds one of the roles in the clinic; no policy means managers.
CREATE FUNCTION clinic_app.workflows_urgent_responsible(clinic uuid,roles varchar[])
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('demographics.read',clinic,NULL) AND EXISTS (
  SELECT 1 FROM clinic_app.identity_userclinicrole r
  WHERE r.user_id=__ACTOR__ AND r.clinic_id=clinic AND r.revoked_at IS NULL
   AND r.organization_id=__TENANT__
   AND r.role=ANY(coalesce(roles,ARRAY['clinic_manager']::varchar[])))
$f$;
-- The latest coverage policy of one clinic, for the bridge and the portal text.
CREATE FUNCTION clinic_app.workflows_coverage_policy(tenant uuid,clinic uuid)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT v.id FROM clinic_app.workflows_urgentcoveragepolicy v
 WHERE v.organization_id=tenant AND v.clinic_id=clinic
 ORDER BY v.version DESC LIMIT 1
$f$;
CREATE FUNCTION clinic_app.workflows_report_urgent(requested_message uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; m RECORD; created uuid;
BEGIN
 SELECT * INTO s FROM clinic_app.current_patient_authority('messages');
 IF s.session_id IS NULL THEN
  RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 -- The session's enrollment names exactly one clinic.
 SELECT x.id,x.conversation_id INTO m FROM clinic_app.comms_message x
  JOIN clinic_app.comms_conversation c ON c.id=x.conversation_id
  WHERE x.id=requested_message AND x.direction='in' AND c.channel='portal'
   AND c.organization_id=s.organization_id AND c.enrollment_id=s.enrollment_id
   AND x.created_at>=transaction_timestamp();
 IF m.id IS NULL THEN
  RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 -- The guard admits a concern only for the message this bridge names.
 PERFORM set_config('app.workflows_urgent_bridge',m.id::text,true);
 INSERT INTO clinic_app.workflows_urgentconcern
  (id,organization_id,clinic_id,enrollment_id,message_id,conversation_id,policy_id,
   state,reported_at,revision)
 SELECT gen_random_uuid(),c.organization_id,c.clinic_id,c.enrollment_id,m.id,c.id,
  clinic_app.workflows_coverage_policy(c.organization_id,c.clinic_id),'open',
  statement_timestamp(),1
 FROM clinic_app.comms_conversation c WHERE c.id=m.conversation_id
 ON CONFLICT (message_id) DO NOTHING RETURNING id INTO created;
 PERFORM set_config('app.workflows_urgent_bridge','',true);
 RETURN created;
END
$f$;
CREATE FUNCTION clinic_app.workflows_patient_coverage()
RETURNS TABLE(weekdays smallint[],opens_at time,closes_at time,response_hours smallint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT v.weekdays::smallint[],v.opens_at,v.closes_at,v.response_hours::smallint
 FROM clinic_app.current_patient_authority('messages') s
 JOIN clinic_app.workflows_urgentcoveragepolicy v
  ON v.id=clinic_app.workflows_coverage_policy(s.organization_id,s.clinic_id)
$f$;
CREATE FUNCTION clinic_app.workflows_urgent_audit_scope(record_id uuid)
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.session_id,s.organization_id,s.clinic_id
 FROM clinic_app.current_patient_authority('messages') s
 JOIN clinic_app.workflows_urgentconcern u ON u.id=record_id
  AND u.organization_id=s.organization_id AND u.enrollment_id=s.enrollment_id
  AND u.reported_at>=transaction_timestamp()
$f$;
CREATE FUNCTION clinic_app.workflows_release_send_eligible(requested_message uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT NOT EXISTS(SELECT 1 FROM clinic_app.workflows_releasedelivery d
  WHERE d.message_id=requested_message AND d.organization_id=__TENANT__
  AND (d.failure_reason<>'' OR NOT clinic_app.orders_is_current(d.version_id)
   OR (d.delegate_grant_id IS NOT NULL AND NOT
    clinic_app.orders_delegate_allowed(d.version_id,d.delegate_grant_id))))
$f$;
CREATE FUNCTION clinic_app.workflows_release_system_task_valid(row_data jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.workflows_releasedelivery d
 WHERE d.id::text=current_setting('app.workflows_release_failure',true)
 AND d.failure_task_id=(row_data->>'id')::uuid AND d.failure_reason<>''
 AND d.organization_id=__TENANT__
 AND row_data->'due_at'=to_jsonb(statement_timestamp()
  +make_interval(hours=>__HOURS__))
 AND (row_data-'id'-'due_at'-'created_at'-'updated_at')=__FOLLOW_UP_ROW__)
$f$;
__TASK_GUARD__
CREATE FUNCTION clinic_app.workflows_release_outcome()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE d clinic_app.workflows_releasedelivery; task uuid := gen_random_uuid();
BEGIN
 -- Only a scoped session records the outcome; the delivery state is derived
 -- from the operation either way.
 IF NEW.organization_id IS DISTINCT FROM __TENANT__ THEN RETURN NULL; END IF;
 SELECT * INTO d FROM clinic_app.workflows_releasedelivery x
  WHERE x.message_id=NEW.subject_id AND x.organization_id=NEW.organization_id
  AND x.failure_reason='' FOR UPDATE;
 IF d.id IS NULL THEN RETURN NULL; END IF;
 PERFORM set_config('app.workflows_release_failure',d.id::text,true);
 UPDATE clinic_app.workflows_releasedelivery SET failure_reason=CASE
  WHEN NEW.last_error='subject_ineligible' THEN 'scope_revoked'
  ELSE 'send_failed' END,failure_task_id=task WHERE id=d.id;
 INSERT INTO clinic_app.workflows_task
  (id,organization_id,clinic_id,kind,subject_ref,created_by_id,owner_user_id,
  owner_role,due_at,priority,state,escalation_policy_version,escalated_at,
  depends_on_id,completion_evidence,revision,idempotency_key,fingerprint,
  last_command_key,last_command_digest,created_at,updated_at)
 SELECT task,x.organization_id,x.clinic_id,'follow_up',
  jsonb_build_object('kind','enrollment','id',x.enrollment_id::text),NULL,NULL,
  'receptionist',statement_timestamp()+make_interval(hours=>__HOURS__),'high',
  'assigned',1,NULL,NULL,'{}',1,x.id,encode(clinic_app.digest(
  'workflows-release-failure:'||x.id::text,'sha256'),'hex'),NULL,'',
  statement_timestamp(),statement_timestamp()
 FROM clinic_app.workflows_releasedelivery x WHERE x.id=d.id;
 PERFORM set_config('app.workflows_release_failure','',true);
 RETURN NULL;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_urgent_responsible(uuid,varchar[]),
 clinic_app.workflows_coverage_policy(uuid,uuid),
 clinic_app.workflows_report_urgent(uuid),clinic_app.workflows_patient_coverage(),
 clinic_app.workflows_urgent_audit_scope(uuid),
 clinic_app.workflows_release_send_eligible(uuid),
 clinic_app.workflows_release_system_task_valid(jsonb),
 clinic_app.workflows_release_outcome() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_report_urgent(uuid),
 clinic_app.workflows_patient_coverage(),
 clinic_app.workflows_release_send_eligible(uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_urgent_responsible(uuid,varchar[])
 TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_urgent_audit_scope(uuid),
 clinic_app.workflows_release_outcome() TO clinic_owner;
RESET ROLE;

CREATE FUNCTION clinic_app.workflows_urgent_audit(record_id uuid,
 occurred_at_utc timestamptz,content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint;
BEGIN
 SELECT * INTO s FROM clinic_app.workflows_urgent_audit_scope(record_id);
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
 SELECT x.organization_id,x.session_id,'workflows.urgent_concern.reported',
  'clinic-os-web',NULL,'workflows.urgent_concern',record_id::text,occurred_at_utc,
  jsonb_build_object('clinic_id',x.clinic_id::text,'object_verb','reported'),
  previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256')
 FROM clinic_app.workflows_urgent_audit_scope(record_id) x
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_urgent_audit(uuid,timestamptz,bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_urgent_audit(uuid,timestamptz,bytea)
 TO clinic_app;

CREATE FUNCTION clinic_app.workflows_coordination_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid := __ACTOR__; r clinic_app.orders_resultrelease;
 c clinic_app.comms_conversation; x clinic_app.comms_message;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'coordination history is retained' USING ERRCODE='23514';
 END IF;
 IF NEW.organization_id IS DISTINCT FROM __TENANT__ THEN
  -- Only the patient bridge runs without a staff tenant, for the one concern
  -- it names (each branch reads only its own table's columns).
  IF TG_TABLE_NAME<>'workflows_urgentconcern' OR TG_OP<>'INSERT' THEN
   RAISE EXCEPTION 'coordination scope mismatch' USING ERRCODE='42501';
  END IF;
  IF current_setting('app.workflows_urgent_bridge',true)
     IS DISTINCT FROM NEW.message_id::text THEN
   RAISE EXCEPTION 'coordination scope mismatch' USING ERRCODE='42501';
  END IF;
 END IF;
 -- Versions are unique per clinic; the services number them under these locks.
 IF TG_TABLE_NAME='workflows_releasenoticetemplate' THEN
  PERFORM pg_advisory_xact_lock(
   hashtextextended('workflows-notice:'||NEW.clinic_id,0));
  IF TG_OP<>'INSERT' OR NEW.approved_by_id IS DISTINCT FROM actor
  OR NOT clinic_app.has_permission('configuration.propose',NEW.clinic_id,NULL)
  OR btrim(NEW.body)='' OR NEW.body~'[0-9@]'
  THEN RAISE EXCEPTION 'notice refused' USING ERRCODE='42501'; END IF;
  NEW.created_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='workflows_urgentcoveragepolicy' THEN
  PERFORM pg_advisory_xact_lock(
   hashtextextended('workflows-coverage:'||NEW.clinic_id,0));
  IF TG_OP<>'INSERT' OR NEW.created_by_id IS DISTINCT FROM actor
  OR NOT clinic_app.has_permission('configuration.clinic',NEW.clinic_id,NULL)
  OR cardinality(NEW.weekdays)=0 OR NOT NEW.weekdays<@ARRAY[0,1,2,3,4,5,6]::smallint[]
  OR cardinality(NEW.responsible_roles)=0
  OR NOT NEW.responsible_roles<@ARRAY[__RESPONSIBLE__]::varchar[]
  OR extract(second FROM NEW.opens_at)<>0
  OR extract(second FROM NEW.closes_at)<>0
  THEN RAISE EXCEPTION 'coverage refused' USING ERRCODE='42501'; END IF;
  NEW.created_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='workflows_releasedelivery' AND TG_OP='UPDATE' THEN
  -- Only the outcome bridge marks a delivery failed, once, with its task.
  IF current_setting('app.workflows_release_failure',true)
     IS DISTINCT FROM OLD.id::text
  OR (to_jsonb(NEW)-'failure_reason'-'failure_task_id')
     IS DISTINCT FROM (to_jsonb(OLD)-'failure_reason'-'failure_task_id')
  OR OLD.failure_reason<>'' OR NEW.failure_task_id IS NULL
  OR NEW.failure_reason NOT IN ('scope_revoked','send_failed')
  THEN RAISE EXCEPTION 'delivery failure refused' USING ERRCODE='42501'; END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='workflows_releasedelivery' THEN
  -- The enrollment names one clinic; the composite keys bind the template.
  SELECT * INTO r FROM clinic_app.orders_resultrelease z WHERE z.id=NEW.release_id;
  SELECT * INTO x FROM clinic_app.comms_message z WHERE z.id=NEW.message_id;
  SELECT * INTO c FROM clinic_app.comms_conversation z WHERE z.id=x.conversation_id;
  -- One decision on the releasing clinician: the release and message are theirs.
  IF NEW.released_by_id IS DISTINCT FROM actor
  OR r.released_by_id IS DISTINCT FROM NEW.released_by_id
  OR r.version_id IS DISTINCT FROM NEW.version_id
  OR r.audience IS DISTINCT FROM NEW.audience
  OR r.delegate_grant_id IS DISTINCT FROM NEW.delegate_grant_id
  OR NOT EXISTS (SELECT 1 FROM clinic_app.orders_resultmatch m
   JOIN clinic_app.orders_order o ON o.id=m.order_id
   WHERE m.version_id=NEW.version_id AND o.enrollment_id=NEW.enrollment_id)
  OR NOT clinic_app.has_permission('result.release',NEW.clinic_id,NEW.enrollment_id)
  OR x.direction IS DISTINCT FROM 'out'
  OR x.sent_by_id IS DISTINCT FROM NEW.released_by_id
  OR c.channel IS DISTINCT FROM 'portal'
  OR c.organization_id IS DISTINCT FROM NEW.organization_id
  OR c.enrollment_id IS DISTINCT FROM NEW.enrollment_id
  OR NEW.failure_reason<>'' OR NEW.failure_task_id IS NOT NULL
  THEN RAISE EXCEPTION 'delivery refused' USING ERRCODE='42501'; END IF;
  NEW.created_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF TG_OP='INSERT' THEN
  -- The runtime has no INSERT grant; only the patient bridge writes concerns.
  IF current_setting('app.workflows_urgent_bridge',true)
     IS DISTINCT FROM NEW.message_id::text OR NEW.state<>'open' OR NEW.revision<>1
  THEN RAISE EXCEPTION 'concern refused' USING ERRCODE='42501'; END IF;
  RETURN NEW;
 END IF;
 IF (to_jsonb(NEW)-'state'-'acknowledged_by_id'-'acknowledged_at'-'resolved_by_id'
     -'resolved_at'-'revision') IS DISTINCT FROM (to_jsonb(OLD)-'state'
     -'acknowledged_by_id'-'acknowledged_at'-'resolved_by_id'-'resolved_at'-'revision')
 OR NEW.revision<>OLD.revision+1
 OR NOT clinic_app.workflows_urgent_responsible(NEW.clinic_id,
  (SELECT p.responsible_roles FROM clinic_app.workflows_urgentcoveragepolicy p
   WHERE p.id=NEW.policy_id))
 OR NOT ((OLD.state='open' AND NEW.state='acknowledged'
   AND NEW.acknowledged_by_id IS NOT DISTINCT FROM actor AND NEW.resolved_by_id IS NULL)
  OR (OLD.state='acknowledged' AND NEW.state='resolved'
   AND NEW.acknowledged_by_id IS NOT DISTINCT FROM OLD.acknowledged_by_id
   AND NEW.resolved_by_id IS NOT DISTINCT FROM actor))
 THEN RAISE EXCEPTION 'concern transition refused' USING ERRCODE='42501'; END IF;
 IF NEW.state='acknowledged' THEN NEW.acknowledged_at:=statement_timestamp();
 ELSE NEW.resolved_at:=statement_timestamp(); END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_coordination_guard()
 FROM PUBLIC, clinic_app, clinic_resolver;
""".replace("__ALL__", _ALL)
    .replace("__ACTOR__", _ACTOR)
    .replace("__RESPONSIBLE__", RESPONSIBLE)
    .replace("__FOLLOW_UP_ROW__", _FOLLOW_UP_ROW)
    .replace("__TASK_GUARD__", TASK_GUARD)
    .replace("__TENANT__", _TENANT)
    .replace("__HOURS__", str(FOLLOW_UP_HOURS))
)

_READ: Final = {
    # result.release is patient-scoped (it needs an enrollment): the clinic-level
    # clinician cell is configuration.propose; a delivery is read with the
    # patient-scoped release authority over its own enrollment.
    "workflows_releasenoticetemplate": (
        "clinic_app.has_permission('configuration.propose',clinic_id,NULL)"
    ),
    "workflows_releasedelivery": (
        "clinic_app.has_permission('result.release',clinic_id,enrollment_id)"
    ),
    "workflows_urgentcoveragepolicy": (
        "clinic_app.has_permission('demographics.read',clinic_id,NULL) OR "
        "clinic_app.has_permission('configuration.clinic',clinic_id,NULL)"
    ),
    "workflows_urgentconcern": (
        "clinic_app.workflows_urgent_responsible(clinic_id,(SELECT p.responsible_roles "
        "FROM clinic_app.workflows_urgentcoveragepolicy p WHERE p.id=policy_id)) OR "
        "clinic_app.has_permission('configuration.clinic',clinic_id,NULL)"
    ),
}
for _table in TABLES:
    SQL += f"""
ALTER TABLE clinic_app.{_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{_table} FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{_table} ADD CONSTRAINT {_table}_clinic_fk
 FOREIGN KEY (organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
CREATE POLICY coordination_owner ON clinic_app.{_table} TO clinic_owner
 USING ({_SCOPE}) WITH CHECK ({_SCOPE});
CREATE POLICY coordination_read ON clinic_app.{_table} FOR SELECT TO clinic_app
 USING ({_SCOPE} AND ({_READ[_table]}));
CREATE TRIGGER coordination_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.{_table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.workflows_coordination_guard();
CREATE TRIGGER coordination_truncate BEFORE TRUNCATE ON clinic_app.{_table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.workflows_coordination_guard();
"""
SQL += f"""
CREATE POLICY coordination_insert ON clinic_app.workflows_releasenoticetemplate
 FOR INSERT TO clinic_app WITH CHECK ({_SCOPE});
CREATE POLICY coordination_insert ON clinic_app.workflows_releasedelivery
 FOR INSERT TO clinic_app WITH CHECK ({_SCOPE});
CREATE POLICY coordination_insert ON clinic_app.workflows_urgentcoveragepolicy
 FOR INSERT TO clinic_app WITH CHECK ({_SCOPE});
CREATE POLICY coordination_update ON clinic_app.workflows_urgentconcern
 FOR UPDATE TO clinic_app USING ({_SCOPE} AND ({_READ["workflows_urgentconcern"]}))
 WITH CHECK ({_SCOPE});
"""
# Every reference stays inside one organization and clinic.
_SCOPED_REFERENCES: Final = (
    ("workflows_releasedelivery", "enrollment", "intake_patientclinicenrollment"),
    ("workflows_releasedelivery", "template", "workflows_releasenoticetemplate"),
    ("workflows_urgentconcern", "enrollment", "intake_patientclinicenrollment"),
    ("workflows_urgentconcern", "policy", "workflows_urgentcoveragepolicy"),
)
for _table, _column, _target in _SCOPED_REFERENCES:
    SQL += f"""
ALTER TABLE clinic_app.{_table} ADD CONSTRAINT {_table}_{_column}_scope_fk
 FOREIGN KEY (organization_id,clinic_id,{_column}_id)
 REFERENCES clinic_app.{_target}(organization_id,clinic_id,id)
 DEFERRABLE INITIALLY DEFERRED;
"""

# The outcome bridge watches only terminal transitions of comms messages.
SQL += """
CREATE TRIGGER workflows_release_outcome AFTER UPDATE OF status
 ON clinic_app.comms_integrationoperation FOR EACH ROW
 WHEN (NEW.status IN ('failed','cancelled') AND OLD.status IS DISTINCT FROM NEW.status
  AND NEW.subject_type='comms.message')
 EXECUTE FUNCTION clinic_app.workflows_release_outcome();
"""

# The schema operations drop the tables after this reverse runs (RunSQL is the
# last operation), so it drops only what depends on the functions it removes.
_REVERSE_TABLE_OBJECTS = "".join(
    f"DROP TRIGGER coordination_guard ON clinic_app.{table};\n"
    f"DROP TRIGGER coordination_truncate ON clinic_app.{table};\n"
    f"DROP POLICY coordination_owner ON clinic_app.{table};\n"
    f"DROP POLICY coordination_read ON clinic_app.{table};\n"
    for table in TABLES
) + "".join(
    # These reference the new tables' scope keys, which the schema reversal
    # drops before the tables themselves.
    f"ALTER TABLE clinic_app.{table} DROP CONSTRAINT {table}_{column}_scope_fk;\n"
    for table, column, _target in _SCOPED_REFERENCES
)
REVERSE_SQL: Final = f"""
DROP TRIGGER workflows_release_outcome ON clinic_app.comms_integrationoperation;
{_REVERSE_TABLE_OBJECTS}
DROP POLICY coordination_insert ON clinic_app.workflows_releasenoticetemplate;
DROP POLICY coordination_insert ON clinic_app.workflows_releasedelivery;
DROP POLICY coordination_insert ON clinic_app.workflows_urgentcoveragepolicy;
DROP POLICY coordination_update ON clinic_app.workflows_urgentconcern;
DROP FUNCTION clinic_app.workflows_urgent_audit(uuid,timestamptz,bytea);
DROP FUNCTION clinic_app.workflows_coordination_guard();
SET LOCAL ROLE clinic_resolver;
{CARE_TASK_GUARD}
DROP FUNCTION clinic_app.workflows_release_outcome();
DROP FUNCTION clinic_app.workflows_release_system_task_valid(jsonb);
DROP FUNCTION clinic_app.workflows_release_send_eligible(uuid);
DROP FUNCTION clinic_app.workflows_urgent_audit_scope(uuid);
DROP FUNCTION clinic_app.workflows_patient_coverage();
DROP FUNCTION clinic_app.workflows_report_urgent(uuid);
DROP FUNCTION clinic_app.workflows_coverage_policy(uuid,uuid);
DROP FUNCTION clinic_app.workflows_urgent_responsible(uuid,varchar[]);
RESET ROLE;
"""
