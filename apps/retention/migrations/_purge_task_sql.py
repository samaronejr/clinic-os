"""One source-backed workflow task for failed destruction, without staff identity."""

SQL = r"""

GRANT INSERT ON clinic_app.workflows_task TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.retention_system_task_valid(row_data jsonb)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT pg_trigger_depth()>1 AND EXISTS(
 SELECT 1 FROM clinic_app.retention_artifactpurge p
 WHERE p.failure_task_id=(row_data->>'id')::uuid AND p.failure_code<>''
 AND p.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND (row_data-'id'-'created_at'-'updated_at')=jsonb_build_object(
  'organization_id',p.organization_id,'clinic_id',p.clinic_id,'kind','review',
  'subject_ref',jsonb_build_object('kind','clinic','id',p.clinic_id::text),
  'created_by_id',NULL,'owner_user_id',NULL,'owner_role','clinic_admin',
  'due_at',p.started_at,'priority','urgent','state','assigned',
  'escalation_policy_version',1,'escalated_at',NULL,'depends_on_id',NULL,
  'completion_evidence','{}'::jsonb,'revision',1,'idempotency_key',p.failure_task_id,
  'fingerprint',encode(clinic_app.digest('retention-task:'||p.id::text,'sha256'),'hex'),
  'last_command_key',NULL,'last_command_digest',''))
$f$;
REVOKE ALL ON FUNCTION clinic_app.retention_system_task_valid(jsonb) FROM PUBLIC;
CREATE FUNCTION clinic_app.retention_emit_task()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF OLD.failure_task_id IS NULL AND NEW.failure_task_id IS NOT NULL
 AND current_setting('app.retention_restore',true) IS DISTINCT FROM 'true' THEN
  INSERT INTO clinic_app.workflows_task
  (id,organization_id,clinic_id,kind,subject_ref,created_by_id,owner_user_id,
   owner_role,due_at,priority,state,escalation_policy_version,escalated_at,
   depends_on_id,completion_evidence,revision,idempotency_key,fingerprint,
   last_command_key,last_command_digest,created_at,updated_at)
  VALUES(NEW.failure_task_id,NEW.organization_id,NEW.clinic_id,'review',
   jsonb_build_object('kind','clinic','id',NEW.clinic_id::text),NULL,NULL,
   'clinic_admin',NEW.started_at,'urgent','assigned',1,NULL,NULL,'{}',1,
   NEW.failure_task_id,encode(clinic_app.digest(
    'retention-task:'||NEW.id::text,'sha256'),'hex'),
   NULL,'',statement_timestamp(),statement_timestamp());
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_emit_task() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_emit_task() TO clinic_owner;

CREATE OR REPLACE FUNCTION clinic_app.workflows_guard()
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


 IF TG_TABLE_NAME='workflows_task' AND TG_OP='INSERT'
 AND clinic_app.retention_system_task_valid(to_jsonb(NEW)) THEN RETURN NEW; END IF;
 IF TG_TABLE_NAME='workflows_task' AND TG_OP='INSERT'
 AND clinic_app.orders_system_task_valid(to_jsonb(NEW)) THEN RETURN NEW; END IF;
  IF TG_TABLE_NAME='workflows_task' THEN
  IF TG_OP='INSERT' THEN
   IF NOT clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)
   OR NEW.created_by_id IS DISTINCT FROM actor OR NEW.state<>'open'
   OR NEW.owner_user_id IS NOT NULL OR NEW.owner_role<>'' OR NEW.revision<>1
   OR NEW.completion_evidence<>'{}' OR NEW.escalated_at IS NOT NULL
   OR NEW.last_command_key IS NOT NULL OR NEW.last_command_digest<>''
   OR (NEW.depends_on_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM clinic_app.workflows_task parent
    WHERE parent.id=NEW.depends_on_id AND parent.id<>NEW.id
    AND parent.clinic_id=NEW.clinic_id
    AND parent.organization_id=NEW.organization_id))
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
    -- owner_valid decides tasks.assign; the UPDATE policy already limits a
    -- non-manager's rows to own open tasks and owned work, so a self-claim
    -- needs no second, unobservable copy of that visibility here.
    IF NOT clinic_app.workflows_owner_valid(NEW.clinic_id, NEW.owner_user_id,
    NEW.owner_role)
    OR NOT (clinic_app.has_permission('tasks.reassign',NEW.clinic_id,NULL)
     OR NEW.owner_user_id IS NOT DISTINCT FROM actor)
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
      AND t.state='done'))
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
  IF NOT clinic_app.has_permission('tasks.reassign', NEW.clinic_id, NULL)
   OR NOT clinic_app.has_permission('tasks.view', NEW.clinic_id, NULL)
   OR NEW.key!~'^[a-z][a-z0-9_-]{0,63}$'
   OR NOT clinic_app.workflows_steps_valid(NEW.steps)
  THEN RAISE EXCEPTION 'definition denied' USING ERRCODE='23514'; END IF;
  IF TG_OP='INSERT' THEN
   PERFORM pg_advisory_xact_lock(hashtextextended(
    'clinic-workflow-definition-v1:'||NEW.clinic_id::text||':'||NEW.key,0));
   IF NEW.created_by_id IS DISTINCT FROM actor OR NEW.state<>'draft'
    OR NEW.published_by_id IS NOT NULL OR NEW.validated_at IS NOT NULL
    OR NEW.published_at IS NOT NULL OR NEW.retired_at IS NOT NULL
    OR NEW.revision<>1 OR NEW.last_command_key IS NOT NULL
    OR NEW.last_command_digest<>'' OR NEW.creation_fingerprint<>NEW.fingerprint
    OR NEW.version<>(SELECT COALESCE(max(d.version),0)+1 FROM
     clinic_app.workflows_workflowdefinitionversion d
     WHERE d.clinic_id=NEW.clinic_id AND d.key=NEW.key)
   THEN RAISE EXCEPTION 'definition denied' USING ERRCODE='23514'; END IF;
   NEW.created_at := statement_timestamp();
  ELSE
   IF NEW.revision<>OLD.revision+1 OR NEW.last_command_key IS NULL
    OR NEW.last_command_key IS NOT DISTINCT FROM OLD.last_command_key
    OR NEW.last_command_digest!~'^[0-9a-f]{64}$'
    OR (to_jsonb(NEW)-ARRAY['state','steps','fingerprint','validated_at',
     'published_by_id','published_at','retired_at','revision','last_command_key',
     'last_command_digest']) IS DISTINCT FROM
     (to_jsonb(OLD)-ARRAY['state','steps','fingerprint','validated_at',
     'published_by_id','published_at','retired_at','revision','last_command_key',
     'last_command_digest'])
   THEN RAISE EXCEPTION 'definition identity is immutable'
    USING ERRCODE='23514'; END IF;
   IF OLD.state IN ('draft','validated') AND NEW.state='draft' THEN
    IF NEW.published_by_id IS NOT NULL OR NEW.published_at IS NOT NULL
     OR NEW.retired_at IS NOT NULL OR NEW.validated_at IS NOT NULL
    THEN RAISE EXCEPTION 'definition denied' USING ERRCODE='23514'; END IF;
   ELSIF OLD.state='draft' AND NEW.state='validated' THEN
    IF ROW(NEW.steps,NEW.fingerprint,NEW.published_by_id,NEW.published_at,
     NEW.retired_at) IS DISTINCT FROM ROW(OLD.steps,OLD.fingerprint,
     OLD.published_by_id,OLD.published_at,OLD.retired_at)
    THEN RAISE EXCEPTION 'definition validation changed content'
     USING ERRCODE='23514'; END IF;
    NEW.validated_at := statement_timestamp();
   ELSIF OLD.state='validated' AND NEW.state='published' THEN
    IF ROW(NEW.steps,NEW.fingerprint,NEW.validated_at,NEW.retired_at)
     IS DISTINCT FROM ROW(OLD.steps,OLD.fingerprint,OLD.validated_at,OLD.retired_at)
     OR NEW.published_by_id IS DISTINCT FROM actor
    THEN RAISE EXCEPTION 'publication changed content' USING ERRCODE='23514'; END IF;
    NEW.published_at := statement_timestamp();
   ELSIF OLD.state='published' AND NEW.state='retired' THEN
    IF ROW(NEW.steps,NEW.fingerprint,NEW.validated_at,NEW.published_by_id,
     NEW.published_at) IS DISTINCT FROM ROW(OLD.steps,OLD.fingerprint,
     OLD.validated_at,OLD.published_by_id,OLD.published_at)
    THEN RAISE EXCEPTION 'published definition is immutable'
     USING ERRCODE='23514'; END IF;
    NEW.retired_at := statement_timestamp();
   ELSE RAISE EXCEPTION 'definition transition denied' USING ERRCODE='23514';
   END IF;
  END IF;
 ELSIF TG_TABLE_NAME='workflows_workflowrun' THEN
  IF TG_OP='INSERT' THEN
   SELECT * INTO definition FROM clinic_app.workflows_workflowdefinitionversion d
    WHERE d.id=NEW.definition_version_id AND d.clinic_id=NEW.clinic_id AND
    d.organization_id=NEW.organization_id;
   IF definition.id IS NULL OR definition.state<>'published'
    OR NEW.started_by_id IS DISTINCT FROM actor OR
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
   IF (to_jsonb(NEW)-ARRAY['state','revision','last_command_key',
    'last_command_digest','updated_at']) IS DISTINCT FROM
   (to_jsonb(OLD)-ARRAY['state','revision','last_command_key',
    'last_command_digest','updated_at'])
    OR NEW.revision<>OLD.revision+1
    OR OLD.state IN ('completed','cancelled') OR NOT (
     (OLD.state='pending' AND NEW.state IN ('running','cancelled')) OR
     (OLD.state IN ('running', 'waiting') AND NEW.state IN ('running', 'waiting',
     'completed', 'failed', 'cancelled'))
     OR (OLD.state='failed' AND NEW.state='pending'))
   THEN RAISE EXCEPTION 'invalid run transition' USING ERRCODE='23514'; END IF;
   IF OLD.state='failed' THEN
    IF OLD.started_by_id IS DISTINCT FROM actor
     OR NEW.last_command_key IS NULL
     OR NEW.last_command_key IS NOT DISTINCT FROM OLD.last_command_key
     OR NEW.last_command_digest!~'^[0-9a-f]{64}$'
     OR NOT EXISTS (SELECT 1 FROM clinic_app.workflows_workflowstep s
      WHERE s.run_id=NEW.id AND s.state='failed')
     OR EXISTS (SELECT 1 FROM clinic_app.workflows_workflowstep s
      WHERE s.run_id=NEW.id AND (s.state='cancelled' OR
       (s.state='failed' AND
        (s.operation_id IS NOT NULL OR s.created_task_id IS NOT NULL))))
    THEN RAISE EXCEPTION 'run retry denied' USING ERRCODE='23514'; END IF;
   ELSIF ROW(NEW.last_command_key,NEW.last_command_digest) IS DISTINCT FROM
    ROW(OLD.last_command_key,OLD.last_command_digest) THEN
    RAISE EXCEPTION 'run command identity is immutable' USING ERRCODE='23514';
   END IF;
   -- tasks.view is decided by the UPDATE policy before any row reaches here.
   IF NOT clinic_app.has_permission('tasks.reassign',NEW.clinic_id,NULL)
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
  SELECT * INTO definition FROM clinic_app.workflows_workflowdefinitionversion d
   WHERE d.id=run.definition_version_id;
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
    OR OLD.state IN ('completed','cancelled')
    OR (OLD.state='failed' AND NOT (
     NEW.state='pending' AND run.state='pending'
     AND OLD.operation_id IS NULL AND OLD.created_task_id IS NULL))
    OR OLD.operation_id IS NOT NULL AND NEW.operation_id IS DISTINCT FROM
    OLD.operation_id
    OR OLD.created_task_id IS NOT NULL AND NEW.created_task_id IS DISTINCT FROM
    OLD.created_task_id
   THEN RAISE EXCEPTION 'step identity is immutable' USING ERRCODE='23514'; END IF;
   IF OLD.state='failed' AND NEW.state='pending' THEN
    IF NEW.fencing_token<>OLD.fencing_token OR NEW.claim_until IS NOT NULL
     OR NEW.wake_at IS NOT NULL OR NEW.error_code<>''
    THEN RAISE EXCEPTION 'invalid step retry' USING ERRCODE='23514'; END IF;
   ELSIF NEW.state='running' THEN
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
  IF NEW.state='completed' THEN
   item := definition.steps->NEW.position;
   IF item->>'handler'='task' AND NOT EXISTS (
    SELECT 1 FROM clinic_app.workflows_task t WHERE t.id=NEW.created_task_id
     AND t.kind=item->>'kind'
     AND t.subject_ref=run.context_refs->((item->>'subject')::int)
     AND t.state='done')
    OR item->>'handler'='timer' AND (
     NEW.wake_at IS NULL OR NEW.wake_at>clinic_app.workflows_now())
    OR item->>'handler'='external' AND NOT EXISTS (
     SELECT 1 FROM clinic_app.comms_integrationoperation o
     WHERE o.id=NEW.operation_id AND o.status='delivered')
    THEN RAISE EXCEPTION 'workflow effect is missing' USING ERRCODE='23514'; END IF;
   END IF;
   IF NEW.state='waiting' THEN
    item := definition.steps->NEW.position;
    IF NEW.wake_at IS NULL
     OR item->>'handler'='task' AND NOT EXISTS (
      SELECT 1 FROM clinic_app.workflows_task t WHERE t.id=NEW.created_task_id
      AND t.kind=item->>'kind'
      AND t.subject_ref=run.context_refs->((item->>'subject')::int)
      AND t.state IN ('assigned','in_progress'))
     OR item->>'handler'='external' AND NEW.operation_id IS NULL
    THEN RAISE EXCEPTION 'workflow waiting receipt is missing'
     USING ERRCODE='23514'; END IF;
   END IF;
  END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.retention_failure(clinic uuid,requested uuid,reason text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF NOT clinic_app.retention_authorized(clinic) OR reason<>'storage_unavailable' THEN
  RAISE EXCEPTION 'retention denied' USING ERRCODE='42501'; END IF;
 UPDATE clinic_app.retention_artifactpurge
 SET failure_code=reason,failure_task_id=coalesce(failure_task_id,gen_random_uuid())
 WHERE id=requested AND clinic_id=clinic AND state='purging';
 IF NOT FOUND THEN
  RAISE EXCEPTION 'retention job denied' USING ERRCODE='23514'; END IF;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_failure(uuid,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_failure(uuid,uuid,text) TO clinic_agent;
RESET ROLE;
CREATE TRIGGER retention_purge_task AFTER UPDATE
 ON clinic_app.retention_artifactpurge FOR EACH ROW
 EXECUTE FUNCTION clinic_app.retention_emit_task();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.retention_emit_task() FROM clinic_owner;
RESET ROLE;

"""
