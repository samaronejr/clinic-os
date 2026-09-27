"""Frozen workflow v1 posture, typed JSON and irreversible history guards."""

TABLES = (
    "workflows_task",
    "workflows_taskcomment",
    "workflows_workflowdefinitionversion",
    "workflows_workflowrun",
    "workflows_workflowstep",
)

SQL = """
REVOKE ALL ON clinic_app.workflows_task, clinic_app.workflows_taskcomment,
 clinic_app.workflows_workflowdefinitionversion, clinic_app.workflows_workflowrun,
 clinic_app.workflows_workflowstep FROM PUBLIC, clinic_app, clinic_agent;
GRANT SELECT,INSERT ON clinic_app.workflows_task, clinic_app.workflows_taskcomment,
 clinic_app.workflows_workflowdefinitionversion, clinic_app.workflows_workflowrun,
 clinic_app.workflows_workflowstep TO clinic_app;
GRANT UPDATE (owner_user_id,owner_role,state,completion_evidence,revision,
 escalated_at,last_command_key,last_command_digest,updated_at)
 ON clinic_app.workflows_task TO clinic_app;
GRANT UPDATE (state,updated_at) ON clinic_app.workflows_workflowrun TO clinic_app;
GRANT UPDATE (state,fencing_token,claim_until,wake_at,operation_id,created_task_id,
 error_code,updated_at) ON clinic_app.workflows_workflowstep TO clinic_app;
GRANT SELECT ON clinic_app.workflows_task,
 clinic_app.workflows_workflowdefinitionversion, clinic_app.workflows_workflowrun,
 clinic_app.workflows_workflowstep TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;

CREATE FUNCTION clinic_app.workflows_owned(clinic uuid, owner_user uuid, owner_role
text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('tasks.view',clinic,NULL) AND (
 COALESCE(owner_user=NULLIF(current_setting('app.current_user_id', true),
 '')::uuid, false)
 OR (owner_role<>'' AND EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
 WHERE r.clinic_id=clinic AND r.organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid
 AND r.user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND r.role=owner_role)))
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_owned(uuid,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_owned(uuid,uuid,text) TO clinic_app;

CREATE FUNCTION clinic_app.workflows_owner_valid(clinic uuid, owner_user uuid,
owner_role text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('tasks.assign',clinic,NULL) AND (
 (owner_user IS NULL AND owner_role=ANY(ARRAY['owner','physician','receptionist',
 'clinic_admin','nurse','allied_professional','scheduler','clinic_manager',
 'finance','org_admin']::text[])) OR (owner_role='' AND owner_user IS NOT NULL
 AND EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
 JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
 WHERE r.clinic_id=clinic AND r.user_id=owner_user AND r.organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid)))
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_owner_valid(uuid,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_owner_valid(uuid, uuid, text) TO
clinic_app;

CREATE FUNCTION clinic_app.workflows_reference_valid(ref jsonb, tenant uuid, clinic
uuid)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE identifier uuid;
BEGIN
 IF jsonb_typeof(ref)<>'object' OR NOT ref ?& ARRAY['kind','id']
 OR ref-ARRAY['kind','id']<>'{}'::jsonb OR jsonb_typeof(ref->'id')<>'string'
 OR (ref->>'id') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
 THEN RETURN false; END IF;
 identifier := (ref->>'id')::uuid;
 RETURN CASE ref->>'kind'
 WHEN 'clinic' THEN identifier=clinic
 WHEN 'enrollment' THEN EXISTS (SELECT 1 FROM
 clinic_app.intake_patientclinicenrollment e
 WHERE e.id=identifier AND e.clinic_id=clinic AND e.organization_id=tenant)
 WHEN 'appointment' THEN EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
 WHERE a.id=identifier AND a.clinic_id=clinic AND a.organization_id=tenant)
 WHEN 'task' THEN EXISTS (SELECT 1 FROM clinic_app.workflows_task t
 WHERE t.id=identifier AND t.clinic_id=clinic AND t.organization_id=tenant)
 ELSE false END;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_reference_valid(jsonb, uuid, uuid) FROM
PUBLIC;

CREATE FUNCTION clinic_app.workflows_steps_valid(steps jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE step jsonb;
BEGIN
 IF jsonb_typeof(steps)<>'array' OR jsonb_array_length(steps) NOT BETWEEN 1 AND 64
 THEN RETURN false; END IF;
 FOR step IN SELECT value FROM jsonb_array_elements(steps) LOOP
  IF jsonb_typeof(step)<>'object' THEN RETURN false; END IF;
  IF step->>'handler'='timer' THEN
   IF NOT step ?& ARRAY['handler','seconds'] OR step-ARRAY['handler','seconds']<>'{}'
   OR jsonb_typeof(step->'seconds')<>'number' OR (step->>'seconds')!~'^[0-9]{1,8}$'
   THEN RETURN false; END IF;
   IF (step->>'seconds')::bigint>31536000 THEN RETURN false; END IF;
  ELSIF step->>'handler'='task' THEN
   IF NOT step ?& ARRAY['handler','kind','subject','due_seconds','owner_role']
   OR step-ARRAY['handler','kind','subject','due_seconds','owner_role']<>'{}'
   OR jsonb_typeof(step->'kind')<>'string'
   OR step->>'kind' NOT IN ('checklist','review','follow_up')
   OR jsonb_typeof(step->'subject')<>'number' OR (step->>'subject')!~'^[0-9]{1,2}$'
   OR jsonb_typeof(step->'due_seconds')<>'number'
   OR (step->>'due_seconds')!~'^[0-9]{1,8}$'
   OR jsonb_typeof(step->'owner_role')<>'string'
   OR step->>'owner_role' NOT IN ('owner','physician','receptionist','clinic_admin',
    'nurse','allied_professional','scheduler','clinic_manager','finance','org_admin')
   THEN RETURN false; END IF;
   IF (step->>'subject')::int>63 OR (step->>'due_seconds')::bigint>31536000
   THEN RETURN false; END IF;
  ELSIF step<>'{"handler":"external","provider":"workflow-synthetic-v1"}'::jsonb
   THEN RETURN false;
  END IF;
 END LOOP;
 RETURN true;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_steps_valid(jsonb) FROM PUBLIC;

CREATE FUNCTION clinic_app.workflows_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 run clinic_app.workflows_workflowrun;
 definition clinic_app.workflows_workflowdefinitionversion;
 ref jsonb; item jsonb; allowed boolean;
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'workflow history is immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.organization_id IS DISTINCT FROM NULLIF(current_setting('app.current_tenant',
 true), '')::uuid
 OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c WHERE c.id=NEW.clinic_id
 AND c.organization_id=NEW.organization_id) THEN
  RAISE EXCEPTION 'workflow scope mismatch' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='workflows_task' THEN
  IF TG_OP='INSERT' THEN
   IF NOT clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)
   OR NEW.created_by_id IS DISTINCT FROM actor OR NEW.state<>'open'
   OR NEW.owner_user_id IS NOT NULL OR NEW.owner_role<>'' OR NEW.revision<>1
   OR NEW.completion_evidence<>'{}' OR NEW.escalated_at IS NOT NULL
   OR NEW.last_command_key IS NOT NULL OR NEW.last_command_digest<>''
   OR NOT clinic_app.workflows_reference_valid(NEW.subject_ref, NEW.organization_id,
   NEW.clinic_id)
   THEN RAISE EXCEPTION 'invalid task' USING ERRCODE='23514'; END IF;
  ELSE
   IF OLD.state IN ('done','cancelled') OR NEW.revision<>OLD.revision+1
   OR NEW.last_command_key IS NULL
   OR NEW.last_command_key IS NOT DISTINCT FROM OLD.last_command_key
   OR NEW.last_command_digest!~'^[0-9a-f]{64}$'
   OR (to_jsonb(NEW)-ARRAY['owner_user_id','owner_role','state','completion_evidence',
    'revision', 'escalated_at', 'last_command_key', 'last_command_digest',
    'updated_at']) IS DISTINCT FROM
    (to_jsonb(OLD)-ARRAY['owner_user_id','owner_role','state','completion_evidence',
    'revision','escalated_at','last_command_key','last_command_digest','updated_at'])
   THEN RAISE EXCEPTION 'task identity is immutable' USING ERRCODE='23514'; END IF;
   IF ROW(NEW.owner_user_id, NEW.owner_role) IS DISTINCT FROM ROW(OLD.owner_user_id,
   OLD.owner_role) THEN
    IF NOT clinic_app.workflows_owner_valid(NEW.clinic_id, NEW.owner_user_id,
    NEW.owner_role)
    OR NOT (clinic_app.has_permission('tasks.reassign',NEW.clinic_id,NULL)
     OR (clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)
      AND NEW.owner_user_id=actor AND ((OLD.state='open' AND OLD.created_by_id=actor)
       OR clinic_app.workflows_owned(OLD.clinic_id,OLD.owner_user_id,OLD.owner_role))))
    THEN RAISE EXCEPTION 'task assignment denied' USING ERRCODE='42501'; END IF;
   END IF;
   allowed := NEW.state=OLD.state OR (OLD.state='open' AND NEW.state='assigned')
    OR (OLD.state='assigned' AND NEW.state='in_progress')
    OR (OLD.state='in_progress' AND NEW.state='done') OR NEW.state='cancelled';
   IF NOT allowed THEN RAISE EXCEPTION 'invalid task transition' USING
   ERRCODE='23514'; END IF;
   IF NEW.state IN ('in_progress','done') AND NEW.state<>OLD.state THEN
    IF NOT clinic_app.has_permission('tasks.complete',NEW.clinic_id,NULL)
    OR NOT clinic_app.workflows_owned(OLD.clinic_id,OLD.owner_user_id,OLD.owner_role)
    OR (NEW.depends_on_id IS NOT NULL AND NOT EXISTS
     (SELECT 1 FROM clinic_app.workflows_task t WHERE t.id=NEW.depends_on_id
      AND t.clinic_id=NEW.clinic_id AND t.state='done'))
    THEN RAISE EXCEPTION 'task completion denied' USING ERRCODE='42501'; END IF;
   END IF;
   IF NEW.state='cancelled' AND NOT clinic_app.has_permission('tasks.reassign',
   NEW.clinic_id, NULL)
   THEN RAISE EXCEPTION 'task cancellation denied' USING ERRCODE='42501'; END IF;
   IF NEW.escalated_at IS DISTINCT FROM OLD.escalated_at AND
    (OLD.escalated_at IS NOT NULL OR NEW.escalated_at IS NULL
     OR NEW.due_at>statement_timestamp()
     OR NOT clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL))
   THEN RAISE EXCEPTION 'invalid escalation' USING ERRCODE='23514'; END IF;
  END IF;
  IF NEW.state='open' AND (NEW.owner_user_id IS NOT NULL OR NEW.owner_role<>'')
   OR NEW.state IN ('assigned', 'in_progress', 'done') AND NEW.owner_user_id IS NULL
   AND NEW.owner_role=''
  THEN RAISE EXCEPTION 'invalid task owner' USING ERRCODE='23514'; END IF;
  IF NEW.state='done' THEN
   IF NEW.kind='checklist' THEN allowed :=
   NEW.completion_evidence='{"checked":true}'::jsonb;
   ELSE allowed := jsonb_typeof(NEW.completion_evidence)='object'
    AND NEW.completion_evidence ?& ARRAY['record','outcome']
    AND NEW.completion_evidence-ARRAY['record','outcome']='{}'
    AND NEW.completion_evidence->>'outcome' IN ('reviewed','resolved','escalated')
    AND clinic_app.workflows_reference_valid(NEW.completion_evidence->'record',
    NEW.organization_id, NEW.clinic_id);
   END IF;
   IF allowed IS DISTINCT FROM true THEN RAISE EXCEPTION 'invalid task evidence' USING
   ERRCODE='23514'; END IF;
  ELSIF NEW.completion_evidence<>'{}' THEN
   RAISE EXCEPTION 'premature task evidence' USING ERRCODE='23514';
  END IF;
 ELSIF TG_TABLE_NAME='workflows_taskcomment' THEN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'comment is immutable' USING
  ERRCODE='23514'; END IF;
  IF NEW.author_id IS DISTINCT FROM actor OR NOT
  clinic_app.has_permission('tasks.view', NEW.clinic_id, NULL)
   OR octet_length(NEW.body)>65536 OR NOT EXISTS
   (SELECT 1 FROM clinic_app.workflows_task t WHERE t.id=NEW.task_id AND
   t.clinic_id=NEW.clinic_id
    AND t.organization_id=NEW.organization_id AND ((t.state='open' AND
    t.created_by_id=actor)
     OR clinic_app.workflows_owned(t.clinic_id,t.owner_user_id,t.owner_role)
     OR clinic_app.has_permission('tasks.reassign',t.clinic_id,NULL)))
  THEN RAISE EXCEPTION 'comment denied' USING ERRCODE='42501'; END IF;
 ELSIF TG_TABLE_NAME='workflows_workflowdefinitionversion' THEN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'definition is immutable' USING
  ERRCODE='23514'; END IF;
  PERFORM
  pg_advisory_xact_lock(hashtextextended('clinic-workflow-definition-v1:'||NEW.clinic_id::text||':'||NEW.key,
  0));
  IF NEW.published_by_id IS DISTINCT FROM actor OR NOT
  clinic_app.has_permission('tasks.reassign', NEW.clinic_id, NULL)
   OR NOT clinic_app.has_permission('tasks.view', NEW.clinic_id, NULL)
   OR NEW.key!~'^[a-z][a-z0-9_-]{0,63}$' OR NOT
   clinic_app.workflows_steps_valid(NEW.steps)
   OR NEW.version<>(SELECT COALESCE(max(d.version), 0)+1 FROM
   clinic_app.workflows_workflowdefinitionversion d
    WHERE d.clinic_id=NEW.clinic_id AND d.key=NEW.key)
  THEN RAISE EXCEPTION 'definition denied' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='workflows_workflowrun' THEN
  IF TG_OP='INSERT' THEN
   SELECT * INTO definition FROM clinic_app.workflows_workflowdefinitionversion d
    WHERE d.id=NEW.definition_version_id AND d.clinic_id=NEW.clinic_id AND
    d.organization_id=NEW.organization_id;
   IF definition.id IS NULL OR NEW.started_by_id IS DISTINCT FROM actor OR
   NEW.state<>'pending'
    OR NOT clinic_app.has_permission('tasks.reassign',NEW.clinic_id,NULL)
    OR NOT clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)
    OR NOT clinic_app.has_permission('tasks.view',NEW.clinic_id,NULL)
    OR jsonb_typeof(NEW.context_refs)<>'array' OR jsonb_array_length(NEW.context_refs)
    NOT BETWEEN 1 AND 64
   THEN RAISE EXCEPTION 'run denied' USING ERRCODE='23514'; END IF;
   FOR ref IN SELECT value FROM jsonb_array_elements(NEW.context_refs) LOOP
    IF NOT clinic_app.workflows_reference_valid(ref,NEW.organization_id,NEW.clinic_id)
    THEN RAISE EXCEPTION 'invalid run reference' USING ERRCODE='23514'; END IF;
   END LOOP;
   FOR item IN SELECT value FROM jsonb_array_elements(definition.steps) LOOP
    IF item->>'handler'='task' AND
    (item->>'subject')::int>=jsonb_array_length(NEW.context_refs)
    THEN RAISE EXCEPTION 'missing run reference' USING ERRCODE='23514'; END IF;
   END LOOP;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['state', 'updated_at']) IS DISTINCT FROM
   (to_jsonb(OLD)-ARRAY['state', 'updated_at'])
    OR OLD.state IN ('completed','failed','cancelled') OR NOT (
     (OLD.state='pending' AND NEW.state IN ('running','cancelled')) OR
     (OLD.state IN ('running', 'waiting') AND NEW.state IN ('running', 'waiting',
     'completed', 'failed', 'cancelled')))
   THEN RAISE EXCEPTION 'invalid run transition' USING ERRCODE='23514'; END IF;
   IF NOT clinic_app.has_permission('tasks.reassign',NEW.clinic_id,NULL)
   OR NOT clinic_app.has_permission('tasks.view',NEW.clinic_id,NULL)
   OR (NEW.state<>'cancelled' AND NOT clinic_app.has_permission('tasks.assign',
   NEW.clinic_id, NULL))
   THEN RAISE EXCEPTION 'run transition denied' USING ERRCODE='42501'; END IF;
   IF NEW.state='completed' AND EXISTS (SELECT 1 FROM
   clinic_app.workflows_workflowstep s
    WHERE s.run_id=NEW.id AND s.state<>'completed')
   THEN RAISE EXCEPTION 'unfinished run' USING ERRCODE='23514'; END IF;
  END IF;
 ELSE
  SELECT * INTO run FROM clinic_app.workflows_workflowrun r WHERE r.id=NEW.run_id
   AND r.clinic_id=NEW.clinic_id AND r.organization_id=NEW.organization_id;
  IF run.id IS NULL OR NOT (run.started_by_id=actor AND
  clinic_app.has_permission('tasks.reassign', NEW.clinic_id, NULL)
  AND clinic_app.has_permission('tasks.assign', NEW.clinic_id, NULL)
  AND clinic_app.has_permission('tasks.view', NEW.clinic_id, NULL)
   OR NEW.state='cancelled' AND clinic_app.has_permission('tasks.reassign',
   NEW.clinic_id, NULL) AND clinic_app.has_permission('tasks.view', NEW.clinic_id,
   NULL))
  THEN RAISE EXCEPTION 'step authority denied' USING ERRCODE='42501'; END IF;
  IF TG_OP='INSERT' THEN
   SELECT * INTO definition FROM clinic_app.workflows_workflowdefinitionversion d
   WHERE d.id=run.definition_version_id;
   IF NEW.position>=jsonb_array_length(definition.steps) OR NEW.state<>'pending'
    OR NEW.fencing_token<>0 OR NEW.claim_until IS NOT NULL OR NEW.wake_at IS NOT NULL
    OR NEW.operation_id IS NOT NULL OR NEW.created_task_id IS NOT NULL OR
    NEW.error_code<>''
   THEN RAISE EXCEPTION 'invalid step' USING ERRCODE='23514'; END IF;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['state', 'fencing_token', 'claim_until', 'wake_at',
   'operation_id', 'created_task_id', 'error_code', 'updated_at'])
    IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state', 'fencing_token', 'claim_until',
    'wake_at', 'operation_id', 'created_task_id', 'error_code', 'updated_at'])
    OR OLD.state IN ('completed','failed','cancelled')
    OR OLD.operation_id IS NOT NULL AND NEW.operation_id IS DISTINCT FROM
    OLD.operation_id
    OR OLD.created_task_id IS NOT NULL AND NEW.created_task_id IS DISTINCT FROM
    OLD.created_task_id
   THEN RAISE EXCEPTION 'step identity is immutable' USING ERRCODE='23514'; END IF;
   IF NEW.state='running' THEN
    IF NEW.fencing_token<>OLD.fencing_token+1 OR NEW.claim_until IS NULL
    THEN RAISE EXCEPTION 'invalid fencing claim' USING ERRCODE='23514'; END IF;
   ELSIF NEW.state IN ('waiting','completed','failed','cancelled') THEN
    IF NEW.fencing_token<>OLD.fencing_token OR (OLD.state<>'running' AND
    NEW.state<>'cancelled')
    THEN RAISE EXCEPTION 'invalid step transition' USING ERRCODE='23514'; END IF;
   ELSE RAISE EXCEPTION 'invalid step transition' USING ERRCODE='23514'; END IF;
  END IF;
  IF NEW.operation_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM
  clinic_app.comms_integrationoperation o
   WHERE o.id=NEW.operation_id AND o.organization_id=NEW.organization_id AND
   o.clinic_id=NEW.clinic_id
    AND o.kind='action' AND o.subject_type='workflows.step' AND o.subject_id=NEW.id
    AND o.actor_id=run.started_by_id)
  THEN RAISE EXCEPTION 'step operation mismatch' USING ERRCODE='23514'; END IF;
  IF NEW.created_task_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM
  clinic_app.workflows_task t
   WHERE t.id=NEW.created_task_id AND t.organization_id=NEW.organization_id AND
   t.clinic_id=NEW.clinic_id
   AND t.idempotency_key=NEW.id)
  THEN RAISE EXCEPTION 'step task mismatch' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_guard() TO clinic_owner;

CREATE FUNCTION clinic_app.workflows_step_scope(requested_step uuid)
RETURNS TABLE(organization_id uuid, clinic_id uuid, actor_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog, clinic_app, pg_temp
AS $f$
 SELECT s.organization_id, s.clinic_id, r.started_by_id FROM
 clinic_app.workflows_workflowstep s
 JOIN clinic_app.workflows_workflowrun r ON r.id=s.run_id WHERE s.id=requested_step
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_step_scope(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_step_scope(uuid) TO clinic_app;

CREATE FUNCTION clinic_app.workflows_due_steps(due_at timestamptz)
RETURNS TABLE(step_id uuid) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.id FROM clinic_app.workflows_workflowstep s JOIN
 clinic_app.workflows_workflowrun r ON r.id=s.run_id
 WHERE r.state IN ('pending', 'running', 'waiting') AND s.state IN ('pending',
 'running', 'waiting')
 AND (s.wake_at IS NULL OR s.wake_at<=due_at)
 AND (s.state<>'running' OR s.claim_until<=due_at)
 AND NOT EXISTS (SELECT 1 FROM clinic_app.workflows_workflowstep previous
 WHERE previous.run_id=s.run_id AND previous.position<s.position AND
 previous.state<>'completed')
 ORDER BY s.wake_at NULLS FIRST,s.id LIMIT 100
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_due_steps(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_due_steps(timestamptz) TO clinic_app;
CREATE FUNCTION clinic_app.workflows_task_scope(requested_task uuid)
RETURNS TABLE(organization_id uuid,clinic_id uuid,actor_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT organization_id,clinic_id,created_by_id FROM clinic_app.workflows_task
 WHERE id=requested_task
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_task_scope(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_task_scope(uuid) TO clinic_app;
CREATE FUNCTION clinic_app.workflows_due_tasks(due_at timestamptz)
RETURNS TABLE(task_id uuid) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT t.id FROM clinic_app.workflows_task t
 WHERE t.state IN ('open','assigned','in_progress') AND t.escalated_at IS NULL
 AND t.due_at<=$1 ORDER BY t.due_at,t.id LIMIT 100
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_due_tasks(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_due_tasks(timestamptz) TO clinic_app;
CREATE FUNCTION clinic_app.workflows_staff_catalog(clinic uuid)
RETURNS TABLE(user_id uuid,display_label text) LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT DISTINCT u.id,u.username::text FROM clinic_app.identity_user u
 JOIN clinic_app.identity_userclinicrole r ON r.user_id=u.id
 WHERE u.is_active AND r.clinic_id=clinic AND r.organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.has_permission('tasks.view',clinic,NULL) ORDER BY u.username::text,u.id
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_staff_catalog(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_staff_catalog(uuid) TO clinic_app;
RESET ROLE;
"""

_SCOPE = "organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid"
_TASK_READ = """clinic_app.has_permission('tasks.view',clinic_id,NULL) AND
 ((state='open' AND
 created_by_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid)
 OR clinic_app.workflows_owned(clinic_id,owner_user_id,owner_role)
 OR clinic_app.has_permission('tasks.reassign',clinic_id,NULL))"""
_RUN_READ = """clinic_app.has_permission('tasks.view',clinic_id,NULL) AND
 (started_by_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 OR clinic_app.has_permission('tasks.reassign',clinic_id,NULL))"""
_READ = {
    "workflows_task": _TASK_READ,
    "workflows_taskcomment": (
        "EXISTS (SELECT 1 FROM clinic_app.workflows_task t WHERE t.id=task_id)"
    ),
    "workflows_workflowdefinitionversion": (
        "clinic_app.has_permission('tasks.view',clinic_id,NULL)"
    ),
    "workflows_workflowrun": _RUN_READ,
    "workflows_workflowstep": (
        "EXISTS (SELECT 1 FROM clinic_app.workflows_workflowrun r WHERE r.id=run_id)"
    ),
}
_INSERT = {
    "workflows_task": "tasks.assign",
    "workflows_taskcomment": "tasks.view",
    "workflows_workflowdefinitionversion": "tasks.reassign",
    "workflows_workflowrun": "tasks.reassign",
    "workflows_workflowstep": "tasks.reassign",
}
for _table in TABLES:
    SQL += f"""
ALTER TABLE clinic_app.{_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{_table} FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{_table} ADD CONSTRAINT {_table}_clinic_fk
 FOREIGN KEY (organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
CREATE POLICY workflow_owner ON clinic_app.{_table} TO clinic_owner
 USING ({_SCOPE}) WITH CHECK ({_SCOPE});
CREATE POLICY workflow_read ON clinic_app.{_table} FOR SELECT TO clinic_app
 USING ({_SCOPE} AND ({_READ[_table]}));
CREATE POLICY workflow_insert ON clinic_app.{_table} FOR INSERT TO clinic_app
 WITH CHECK ({_SCOPE} AND
 clinic_app.has_permission('{_INSERT[_table]}',clinic_id,NULL));
CREATE TRIGGER workflow_guard BEFORE INSERT OR UPDATE OR DELETE ON clinic_app.{_table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.workflows_guard();
"""
    if _table in {"workflows_task", "workflows_workflowrun", "workflows_workflowstep"}:
        SQL += f"""
CREATE POLICY workflow_update ON clinic_app.{_table} FOR UPDATE TO clinic_app
 USING ({_SCOPE} AND ({_READ[_table]}))
 WITH CHECK ({_SCOPE} AND clinic_app.has_permission('tasks.view',clinic_id,NULL));
"""
SQL += """
ALTER TABLE clinic_app.workflows_task ADD CONSTRAINT workflows_task_dependency_fk
 FOREIGN KEY (organization_id,clinic_id,depends_on_id)
 REFERENCES clinic_app.workflows_task(organization_id,clinic_id,id);
ALTER TABLE clinic_app.workflows_taskcomment ADD CONSTRAINT workflows_comment_task_fk
 FOREIGN KEY (organization_id,clinic_id,task_id)
 REFERENCES clinic_app.workflows_task(organization_id,clinic_id,id);
ALTER TABLE clinic_app.workflows_workflowrun ADD CONSTRAINT workflows_run_definition_fk
 FOREIGN KEY (organization_id,clinic_id,definition_version_id)
 REFERENCES clinic_app.workflows_workflowdefinitionversion
 (organization_id,clinic_id,id);
ALTER TABLE clinic_app.workflows_workflowstep ADD CONSTRAINT workflows_step_run_fk
 FOREIGN KEY (organization_id,clinic_id,run_id)
 REFERENCES clinic_app.workflows_workflowrun(organization_id,clinic_id,id);
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.workflows_guard() FROM clinic_owner;
RESET ROLE;
"""

REVERSE_SQL = """
ALTER TABLE clinic_app.workflows_workflowstep DROP CONSTRAINT workflows_step_run_fk;
ALTER TABLE clinic_app.workflows_workflowrun DROP CONSTRAINT
workflows_run_definition_fk;
ALTER TABLE clinic_app.workflows_taskcomment DROP CONSTRAINT workflows_comment_task_fk;
ALTER TABLE clinic_app.workflows_task DROP CONSTRAINT workflows_task_dependency_fk;
"""
for _table in reversed(TABLES):
    REVERSE_SQL += f"""
DROP TRIGGER workflow_guard ON clinic_app.{_table};
DROP POLICY workflow_owner ON clinic_app.{_table};
DROP POLICY workflow_read ON clinic_app.{_table};
DROP POLICY workflow_insert ON clinic_app.{_table};
DROP POLICY IF EXISTS workflow_update ON clinic_app.{_table};
ALTER TABLE clinic_app.{_table} DROP CONSTRAINT {_table}_clinic_fk;
"""
REVERSE_SQL += """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION IF EXISTS clinic_app.workflows_staff_catalog(uuid);
DROP FUNCTION IF EXISTS clinic_app.workflows_task_scope(uuid);
DROP FUNCTION IF EXISTS clinic_app.workflows_due_tasks(timestamptz);
DROP FUNCTION clinic_app.workflows_due_steps(timestamptz);
DROP FUNCTION clinic_app.workflows_step_scope(uuid);
DROP FUNCTION clinic_app.workflows_guard();
DROP FUNCTION clinic_app.workflows_steps_valid(jsonb);
DROP FUNCTION clinic_app.workflows_reference_valid(jsonb,uuid,uuid);
DROP FUNCTION clinic_app.workflows_owner_valid(uuid,uuid,text);
DROP FUNCTION clinic_app.workflows_owned(uuid,uuid,text);
RESET ROLE;
REVOKE SELECT ON clinic_app.workflows_task,
 clinic_app.workflows_workflowdefinitionversion, clinic_app.workflows_workflowrun,
 clinic_app.workflows_workflowstep FROM clinic_resolver;
"""
