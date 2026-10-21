"""Source-backed system tasks reuse the workflow queue without staff impersonation."""

from apps.workflows.migrations._workflow_sql import SQL as WORKFLOW_SQL

FUNCTIONS = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.orders_system_task_valid(row_data jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT pg_trigger_depth()>1 AND EXISTS(
 SELECT 1 FROM clinic_app.orders_resultworkitem w
 JOIN clinic_app.orders_resultversion v ON v.id=w.version_id
 LEFT JOIN clinic_app.orders_resultmatch m ON m.version_id=v.id
 LEFT JOIN clinic_app.orders_order o ON o.id=m.order_id
 WHERE w.task_id=(row_data->>'id')::uuid AND w.organization_id=__TENANT__
 AND (row_data-'id'-'created_at'-'updated_at')=jsonb_build_object(
  'organization_id',w.organization_id,'clinic_id',w.clinic_id,'kind','review',
  'subject_ref',jsonb_build_object('kind','clinic','id',w.clinic_id::text),
  'created_by_id',NULL,'owner_user_id',CASE WHEN w.kind='review'
   THEN o.ordered_by_id ELSE NULL END,
  'owner_role',CASE WHEN w.kind='quarantine' THEN 'receptionist' ELSE '' END,
  'due_at',v.created_at,'priority',CASE WHEN v.critical THEN 'urgent'
   WHEN w.kind='quarantine' THEN 'high' ELSE 'normal' END,
  'state','assigned','escalation_policy_version',1,'escalated_at',NULL,
  'depends_on_id',NULL,'completion_evidence','{}'::jsonb,'revision',1,
  'idempotency_key',w.task_id,'fingerprint',encode(clinic_app.digest(
   'orders-task:'||v.id::text||':'||w.kind,'sha256'),'hex'),
  'last_command_key',NULL,'last_command_digest',''))
$f$;
REVOKE ALL ON FUNCTION clinic_app.orders_system_task_valid(jsonb) FROM PUBLIC;
CREATE FUNCTION clinic_app.orders_emit_task()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE v clinic_app.orders_resultversion; task uuid:=gen_random_uuid();
 task_kind text; clinician uuid;
BEGIN
 SELECT * INTO v FROM clinic_app.orders_resultversion r WHERE r.id=NEW.version_id;
 task_kind:=CASE WHEN TG_TABLE_NAME='orders_resultmatch'
  THEN 'review' ELSE 'quarantine' END;
 IF task_kind='review' THEN
  SELECT o.ordered_by_id INTO clinician FROM clinic_app.orders_order o
  WHERE o.id=NEW.order_id;
 END IF;
 INSERT INTO clinic_app.orders_resultworkitem
 (id,organization_id,clinic_id,created_at,version_id,task_id,kind)
 VALUES(gen_random_uuid(),v.organization_id,v.clinic_id,statement_timestamp(),
 v.id,task,task_kind);
 INSERT INTO clinic_app.workflows_task
 (id,organization_id,clinic_id,kind,subject_ref,created_by_id,owner_user_id,
 owner_role,due_at,priority,state,escalation_policy_version,escalated_at,
 depends_on_id,completion_evidence,revision,idempotency_key,fingerprint,
 last_command_key,last_command_digest,created_at,updated_at)
 VALUES(task,v.organization_id,v.clinic_id,'review',
 jsonb_build_object('kind','clinic','id',v.clinic_id::text),NULL,clinician,
 CASE WHEN task_kind='quarantine' THEN 'receptionist' ELSE '' END,
 v.created_at,CASE WHEN v.critical THEN 'urgent'
 WHEN task_kind='quarantine' THEN 'high' ELSE 'normal' END,
 'assigned',1,NULL,NULL,'{}',1,task,encode(clinic_app.digest(
 'orders-task:'||v.id::text||':'||task_kind,'sha256'),'hex'),
 NULL,'',statement_timestamp(),statement_timestamp());
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.orders_emit_task() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.orders_emit_task() TO clinic_owner;
RESET ROLE;
"""

TRIGGERS = """
CREATE TRIGGER orders_task_after_match AFTER INSERT
 ON clinic_app.orders_resultmatch FOR EACH ROW
 EXECUTE FUNCTION clinic_app.orders_emit_task();
CREATE TRIGGER orders_task_after_quarantine AFTER INSERT
 ON clinic_app.orders_resultquarantine FOR EACH ROW
 EXECUTE FUNCTION clinic_app.orders_emit_task();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.orders_emit_task() FROM clinic_owner;
RESET ROLE;
"""

_GUARD_START = WORKFLOW_SQL.index("CREATE FUNCTION clinic_app.workflows_guard()")
_GUARD_END = WORKFLOW_SQL.index("$f$;", _GUARD_START) + len("$f$;")
_TASK_BRANCH = " IF TG_TABLE_NAME='workflows_task' THEN"
WORKFLOW_BRIDGE = (
    WORKFLOW_SQL[_GUARD_START:_GUARD_END]
    .replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    .replace(
        _TASK_BRANCH,
        """
 IF TG_TABLE_NAME='workflows_task' AND TG_OP='INSERT'
 AND clinic_app.orders_system_task_valid(to_jsonb(NEW)) THEN RETURN NEW; END IF;
 """
        + _TASK_BRANCH,
    )
)
