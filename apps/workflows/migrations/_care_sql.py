"""Care-plan posture: least-privilege grants, FORCE RLS and one transition guard.

Insert authority is decided once, by the BEFORE trigger (it runs before any
INSERT WITH CHECK). Authority is the caller's tasks/clinical permissions read
from the transaction GUCs; the trigger never trusts a column for who acted.

Composite (organization, clinic, id) keys bind every reference from a care row
to its plan, action, assignment, task or enrollment to one clinic, so the guard
never repeats that comparison. The same guard also runs on ``workflows_task``:
a care-owned task cannot be assigned, started or completed while its action
awaits review or its source is stale, and at commit the action must mirror its
task's owner and state, whichever path wrote the task.
"""

from typing import Final

from apps.intake.migrations._data_request_sql import GUARD_SQL as DEPLOYED_GUARD_SQL
from apps.workflows.care import vocab

TABLES = (
    "workflows_careplan",
    "workflows_careaction",
    "workflows_referral",
    "workflows_recall",
    "workflows_proassignment",
    "workflows_prooccurrence",
)
_ALL = ", ".join(f"clinic_app.{table}" for table in TABLES)
_ROLES = (
    "ARRAY['owner','physician','receptionist','clinic_admin','nurse',"
    "'allied_professional','scheduler','clinic_manager','finance','org_admin']::text[]"
)
_RESPONSE_ROLES = "ARRAY['physician','nurse','allied_professional']::text[]"
_ACTOR = "NULLIF(current_setting('app.current_user_id',true),'')::uuid"
_SCOPE = "organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid"


def mirrors(action: str) -> str:
    """Return the predicate under which ``action`` mirrors its task ``t``.

    An open task leaves its action open or overdue; a working task leaves it
    assigned or overdue; a terminal task and its action end in the same state.
    Both always carry the same owner.
    """
    return (
        f"ROW({action}.owner_user_id,{action}.owner_role) IS NOT DISTINCT FROM "
        "ROW(t.owner_user_id,t.owner_role) AND ("
        f"(t.state='open' AND {action}.state IN ('open','overdue'))"
        f" OR (t.state IN ('assigned','in_progress') AND {action}.state IN "
        "('assigned','overdue'))"
        f" OR (t.state IN ('done','cancelled') AND {action}.state=t.state))"
    )


# The task guard admits the PRO review task as a source-backed system task, next
# to the orders and retention ones; derived from the deployed text: intake
# 0026's data-subject-request bridge over retention's guard.
_GUARD_START = DEPLOYED_GUARD_SQL.index(
    "CREATE OR REPLACE FUNCTION clinic_app.workflows_guard()"
)
_GUARD_END = DEPLOYED_GUARD_SQL.index("$f$;", _GUARD_START) + len("$f$;")
_ORDERS_RETURN = (
    " IF TG_TABLE_NAME='workflows_task' AND TG_OP='INSERT'\n"
    " AND clinic_app.orders_system_task_valid(to_jsonb(NEW)) THEN RETURN NEW; END IF;\n"
)
_CARE_RETURN = (
    " IF TG_TABLE_NAME='workflows_task' AND TG_OP='INSERT'\n"
    " AND clinic_app.workflows_care_system_task_valid(to_jsonb(NEW))"
    " THEN RETURN NEW; END IF;\n"
)
TASK_GUARD: Final = DEPLOYED_GUARD_SQL[_GUARD_START:_GUARD_END].replace(
    _ORDERS_RETURN, _ORDERS_RETURN + _CARE_RETURN, 1
)
if len(TASK_GUARD.split(_CARE_RETURN)) - 1 != 1:
    msg = "the deployed task guard changed shape"
    raise RuntimeError(msg)

_sql = """
REVOKE ALL ON __ALL__ FROM PUBLIC, clinic_app, clinic_agent;
GRANT SELECT,INSERT ON __ALL__ TO clinic_app;
GRANT SELECT ON __ALL__ TO clinic_resolver;
GRANT UPDATE (state,revision,last_command_key,last_command_digest,updated_at)
 ON clinic_app.workflows_careplan, clinic_app.workflows_proassignment TO clinic_app;
GRANT UPDATE (owner_user_id,owner_role,state,review_state,revision,last_command_key,
 last_command_digest,updated_at) ON clinic_app.workflows_careaction TO clinic_app;
GRANT UPDATE (status,document_id,revision,last_command_key,last_command_digest,
 updated_at) ON clinic_app.workflows_referral TO clinic_app;
GRANT UPDATE (state,task_id,revision,last_command_key,last_command_digest,updated_at)
 ON clinic_app.workflows_recall TO clinic_app;
GRANT UPDATE (state,answers,concerning,response_task_id,recorded_at,revision,
 last_command_key,last_command_digest,updated_at)
 ON clinic_app.workflows_prooccurrence TO clinic_app;
REVOKE ALL ON clinic_app.workflows_carerefusal FROM PUBLIC, clinic_app, clinic_agent;
GRANT SELECT ON clinic_app.workflows_carerefusal TO clinic_app;
GRANT SELECT,INSERT ON clinic_app.workflows_carerefusal TO clinic_resolver;
GRANT UPDATE (actor_id,attempts,refused_at,retry_at)
 ON clinic_app.workflows_carerefusal TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
""".replace("__ALL__", _ALL)

_sql += (
    """
CREATE FUNCTION clinic_app.workflows_care_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 now_ timestamptz := clinic_app.workflows_now();
 v_plan clinic_app.workflows_careplan;
 v_asg clinic_app.workflows_proassignment;
 v_ver clinic_app.ehr_clinicaldocumentversion;
 v_enc clinic_app.ehr_encounter;
 v_doc clinic_app.prescription_prescriptiondraft;
 allowed boolean;
BEGIN
 IF TG_TABLE_NAME='workflows_prooccurrence' AND TG_WHEN='AFTER' THEN
  -- A recorded response always reaches its response role's human queue as one
  -- source-backed review task; concern only raises the priority. No staff
  -- identity routes it, so a nurse or clinician can record without manager
  -- authority. workflows_care_system_task_valid admits exactly this row.
  SELECT * INTO v_asg FROM clinic_app.workflows_proassignment a
   WHERE a.id=NEW.assignment_id;
  INSERT INTO clinic_app.workflows_task
   (id,organization_id,clinic_id,kind,subject_ref,created_by_id,owner_user_id,
   owner_role,due_at,priority,state,escalation_policy_version,escalated_at,
   depends_on_id,completion_evidence,revision,idempotency_key,fingerprint,
   last_command_key,last_command_digest,created_at,updated_at)
  VALUES (NEW.response_task_id,NEW.organization_id,NEW.clinic_id,'review',
   jsonb_build_object('kind','enrollment','id',v_asg.enrollment_id::text),NULL,NULL,
   v_asg.response_role,NEW.recorded_at,
   CASE WHEN NEW.concerning THEN 'urgent' ELSE 'normal' END,'assigned',1,NULL,NULL,
   '{}',1,NEW.id,encode(clinic_app.digest('workflows-pro-review:'||NEW.id::text,
   'sha256'),'hex'),NULL,'',statement_timestamp(),statement_timestamp());
  RETURN NULL;
 END IF;
 IF TG_TABLE_NAME='workflows_task' THEN
  -- Deferred to commit: whichever path wrote a care-owned task, its action
  -- must end the transaction mirroring the task's owner and state.
  IF TG_WHEN='AFTER' THEN
   IF EXISTS (SELECT 1 FROM clinic_app.workflows_careaction a
    JOIN clinic_app.workflows_task t ON t.id=a.task_id
    WHERE a.task_id=NEW.id AND NOT (__MIRRORS_A__))
   THEN RAISE EXCEPTION 'care task left its action behind' USING ERRCODE='23514';
   END IF;
   RETURN NULL;
  END IF;
  -- The trigger fires only for a new owner or a step towards completion.
  IF EXISTS (SELECT 1 FROM clinic_app.workflows_careaction a
   JOIN clinic_app.workflows_careplan p ON p.id=a.plan_id
   WHERE a.task_id=NEW.id AND (a.review_state='required'
   OR (a.review_state<>'confirmed' AND NOT EXISTS (SELECT 1
    FROM clinic_app.ehr_clinicaldocumentversion v
    WHERE v.id=p.source_version_id AND v.state='finalized'))))
  THEN RAISE EXCEPTION 'care task awaits clinician review' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'care history is immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.organization_id IS DISTINCT FROM NULLIF(current_setting('app.current_tenant',
 true),'')::uuid THEN
  RAISE EXCEPTION 'care scope mismatch' USING ERRCODE='23514';
 END IF;
 IF TG_OP='UPDATE' THEN
  IF NEW.revision<>OLD.revision+1 OR NEW.last_command_key IS NULL
  OR NEW.last_command_key IS NOT DISTINCT FROM OLD.last_command_key
  OR NEW.last_command_digest!~'^[0-9a-f]{64}$' THEN
   RAISE EXCEPTION 'care command identity is invalid' USING ERRCODE='23514';
  END IF;
 ELSIF NEW.revision<>1 OR NEW.last_command_key IS NOT NULL
 OR NEW.last_command_digest<>'' THEN
  RAISE EXCEPTION 'care command identity is invalid' USING ERRCODE='23514';
 END IF;

 IF TG_TABLE_NAME='workflows_careplan' THEN
  IF TG_OP='INSERT' THEN
   SELECT * INTO v_ver FROM clinic_app.ehr_clinicaldocumentversion v
    WHERE v.id=NEW.source_version_id AND v.organization_id=NEW.organization_id;
   SELECT * INTO v_enc FROM clinic_app.ehr_encounter e
    WHERE e.id=NEW.encounter_id AND e.clinic_id=NEW.clinic_id
    AND e.organization_id=NEW.organization_id;
   IF v_ver.id IS NULL OR v_enc.id IS NULL OR v_ver.state<>'finalized'
    OR NOT EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d
     WHERE d.id=v_ver.document_id AND d.encounter_id=v_enc.id)
    OR NOT EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment n
     WHERE n.id=NEW.enrollment_id AND n.patient_id=v_enc.patient_id)
    OR NEW.clinician_id IS DISTINCT FROM actor OR NEW.state<>'active'
    OR NOT clinic_app.has_permission('clinical.write',NEW.clinic_id,NEW.enrollment_id)
   THEN RAISE EXCEPTION 'invalid care plan' USING ERRCODE='23514'; END IF;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['state','revision','last_command_key','last_command_digest',
    'updated_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','revision',
    'last_command_key','last_command_digest','updated_at'])
    OR OLD.state<>'active' OR NEW.state<>'superseded'
    OR EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
     WHERE v.id=NEW.source_version_id AND v.state='finalized')
    OR NOT clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)
   THEN RAISE EXCEPTION 'invalid care plan transition' USING ERRCODE='23514'; END IF;
  END IF;

 ELSIF TG_TABLE_NAME='workflows_careaction' THEN
  SELECT * INTO v_plan FROM clinic_app.workflows_careplan p WHERE p.id=NEW.plan_id;
  IF v_plan.id IS NULL THEN
   RAISE EXCEPTION 'care action plan is missing' USING ERRCODE='23514';
  END IF;
  IF TG_OP='INSERT' THEN
   IF v_plan.state<>'active' OR NEW.origin_version_id<>v_plan.source_version_id
    OR NEW.created_by_id IS DISTINCT FROM actor OR NEW.state<>'open'
    OR NEW.owner_user_id IS NOT NULL OR NEW.owner_role<>'' OR NEW.review_state<>'none'
    OR NOT clinic_app.has_permission('tasks.view',NEW.clinic_id,NULL)
   THEN RAISE EXCEPTION 'invalid care action' USING ERRCODE='23514'; END IF;
   IF NEW.kind IN ('return_window','staff_coordination') THEN
    IF NEW.approved_by_id IS NOT NULL
     OR NOT clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)
    THEN RAISE EXCEPTION 'care action denied' USING ERRCODE='42501'; END IF;
   ELSIF NEW.approved_by_id IS DISTINCT FROM actor OR NOT
    clinic_app.has_permission('clinical.write',NEW.clinic_id,v_plan.enrollment_id)
   THEN RAISE EXCEPTION 'care action requires clinician approval'
    USING ERRCODE='42501'; END IF;
   IF NEW.task_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM clinic_app.workflows_task t
    WHERE t.id=NEW.task_id AND t.idempotency_key=NEW.id AND t.state='open'
    AND t.kind='follow_up' AND t.due_at=NEW.window_end
    AND t.subject_ref->>'kind'='enrollment'
    AND t.subject_ref->>'id'=v_plan.enrollment_id::text)
   THEN RAISE EXCEPTION 'care action task mismatch' USING ERRCODE='23514'; END IF;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['owner_user_id','owner_role','state','review_state',
    'revision','last_command_key','last_command_digest','updated_at'])
    IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['owner_user_id','owner_role','state',
    'review_state','revision','last_command_key','last_command_digest','updated_at'])
    OR OLD.state IN ('done','cancelled')
   THEN RAISE EXCEPTION 'care action identity is immutable'
    USING ERRCODE='23514'; END IF;
   IF NEW.review_state IS DISTINCT FROM OLD.review_state THEN
    IF OLD.review_state='none' AND NEW.review_state='required' THEN
     IF v_plan.state<>'superseded' OR NEW.state<>OLD.state OR NOT
      clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)
     THEN RAISE EXCEPTION 'review flag denied' USING ERRCODE='42501'; END IF;
    ELSIF OLD.review_state='required' AND NEW.review_state='confirmed' THEN
     IF NEW.state<>OLD.state OR NOT clinic_app.has_permission('clinical.write',
      NEW.clinic_id,v_plan.enrollment_id)
     THEN RAISE EXCEPTION 'review decision denied' USING ERRCODE='42501'; END IF;
    ELSE RAISE EXCEPTION 'invalid review transition' USING ERRCODE='23514';
    END IF;
   ELSIF OLD.review_state='required' AND (NEW.state NOT IN (OLD.state,'cancelled')
    OR ROW(NEW.owner_user_id,NEW.owner_role) IS DISTINCT FROM
     ROW(OLD.owner_user_id,OLD.owner_role))
   THEN RAISE EXCEPTION 'care action awaits clinician review'
    USING ERRCODE='23514'; END IF;
   IF NEW.state<>OLD.state THEN
    allowed := (OLD.state='open' AND NEW.state IN ('assigned','overdue','cancelled'))
     OR (OLD.state='open' AND NEW.state='done' AND NEW.task_id IS NULL)
     OR (OLD.state='assigned' AND NEW.state IN ('overdue','done','cancelled'))
     OR (OLD.state='overdue' AND NEW.state IN ('done','cancelled'));
    IF NOT allowed THEN RAISE EXCEPTION 'invalid care action transition'
     USING ERRCODE='23514'; END IF;
    IF NEW.state='overdue' AND (NEW.window_end>now_ OR NOT
     clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL))
    THEN RAISE EXCEPTION 'invalid overdue marking' USING ERRCODE='23514'; END IF;
    IF NEW.state IN ('done','cancelled') AND NEW.task_id IS NULL AND NOT
     clinic_app.has_permission('clinical.write',NEW.clinic_id,v_plan.enrollment_id)
    THEN RAISE EXCEPTION 'clinician decision required' USING ERRCODE='42501';
    END IF;
   END IF;
   IF ((NEW.state IN ('assigned','done') AND NEW.state<>OLD.state)
    OR ROW(NEW.owner_user_id,NEW.owner_role) IS DISTINCT FROM
     ROW(OLD.owner_user_id,OLD.owner_role)) AND NEW.review_state<>'confirmed'
    AND NOT EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
     WHERE v.id=v_plan.source_version_id AND v.state='finalized')
   THEN RAISE EXCEPTION 'care action source is stale' USING ERRCODE='23514'; END IF;
   -- A task-owned action only ever follows its task: owner and state move on
   -- the task first, in the same transaction, never on the action alone.
   IF NEW.task_id IS NOT NULL AND NOT EXISTS (SELECT 1
    FROM clinic_app.workflows_task t WHERE t.id=NEW.task_id AND (__MIRRORS_NEW__))
   THEN RAISE EXCEPTION 'care action must follow its task' USING ERRCODE='23514';
   END IF;
  END IF;

 ELSIF TG_TABLE_NAME='workflows_referral' THEN
  IF NEW.plan_id IS NOT NULL THEN
   SELECT * INTO v_plan FROM clinic_app.workflows_careplan p WHERE p.id=NEW.plan_id;
   IF v_plan.id IS NULL THEN
    RAISE EXCEPTION 'referral plan is missing' USING ERRCODE='23514'; END IF;
  END IF;
  IF NEW.document_id IS NOT NULL THEN
   SELECT * INTO v_doc FROM clinic_app.prescription_prescriptiondraft d
    WHERE d.id=NEW.document_id AND d.clinic_id=NEW.clinic_id
    AND d.organization_id=NEW.organization_id AND d.kind='referral';
   IF v_doc.id IS NULL OR (NEW.plan_id IS NOT NULL
    AND v_doc.encounter_id<>v_plan.encounter_id)
   THEN RAISE EXCEPTION 'referral document mismatch' USING ERRCODE='23514'; END IF;
  END IF;
  IF TG_OP='INSERT' THEN
   IF NEW.created_by_id IS DISTINCT FROM actor THEN
    RAISE EXCEPTION 'invalid referral' USING ERRCODE='23514'; END IF;
   -- Runtime referrals are planned; documents that predate plans were linked
   -- once by migration 0007 (trusted operator code).
   IF NEW.plan_id IS NULL OR NEW.status<>'requested' OR NEW.target_kind='unspecified'
    OR NEW.target_label IS NULL OR NOT EXISTS (SELECT 1
     FROM clinic_app.workflows_careaction a WHERE a.id=NEW.action_id
     AND a.plan_id=NEW.plan_id AND a.kind='referral')
    OR NOT clinic_app.has_permission('clinical.write',NEW.clinic_id,
     v_plan.enrollment_id)
   THEN RAISE EXCEPTION 'invalid referral' USING ERRCODE='42501'; END IF;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['status','document_id','revision','last_command_key',
    'last_command_digest','updated_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY[
    'status','document_id','revision','last_command_key','last_command_digest',
    'updated_at'])
   THEN RAISE EXCEPTION 'referral identity is immutable' USING ERRCODE='23514'; END IF;
   IF NEW.document_id IS DISTINCT FROM OLD.document_id AND (OLD.document_id IS NOT NULL
    OR OLD.plan_id IS NULL OR NOT clinic_app.has_permission('clinical.write',
     NEW.clinic_id,v_plan.enrollment_id))
   THEN RAISE EXCEPTION 'referral link denied' USING ERRCODE='42501'; END IF;
   IF NEW.status IS DISTINCT FROM OLD.status THEN
    allowed := (OLD.status='requested' AND NEW.status IN ('scheduled','cancelled'))
     OR (OLD.status='scheduled' AND NEW.status IN ('completed','cancelled'))
     OR (OLD.status='documented' AND NEW.status IN ('scheduled','cancelled'));
    IF NOT allowed OR NOT clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)
    THEN RAISE EXCEPTION 'invalid referral transition' USING ERRCODE='23514'; END IF;
   END IF;
  END IF;

 ELSIF TG_TABLE_NAME='workflows_recall' THEN
  IF TG_OP='INSERT' THEN
   IF NEW.created_by_id IS DISTINCT FROM actor OR NEW.state<>'scheduled'
    OR NEW.task_id IS NOT NULL OR NOT (NEW.owner_role=ANY(__ROLES__))
    OR NEW.due_at<>NEW.anchor_at+NEW.interval_days*interval '1 day'
    OR NEW.anchor_at NOT BETWEEN now_-interval '5 minutes'
     AND now_+interval '5 minutes'
    OR NOT clinic_app.has_permission('tasks.reassign',NEW.clinic_id,NULL)
   THEN RAISE EXCEPTION 'invalid recall' USING ERRCODE='23514'; END IF;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['state','task_id','revision','last_command_key',
    'last_command_digest','updated_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state',
    'task_id','revision','last_command_key','last_command_digest','updated_at'])
    OR OLD.state<>'scheduled' OR NEW.state<>'materialized' OR NEW.due_at>now_
    OR NOT clinic_app.has_permission('tasks.reassign',NEW.clinic_id,NULL)
    OR NOT EXISTS (SELECT 1 FROM clinic_app.workflows_task t WHERE t.id=NEW.task_id
     AND t.idempotency_key=NEW.id AND t.owner_role=NEW.owner_role
     AND t.kind='follow_up' AND t.subject_ref->>'kind'='enrollment'
     AND t.subject_ref->>'id'=NEW.enrollment_id::text)
   THEN RAISE EXCEPTION 'invalid recall transition' USING ERRCODE='23514'; END IF;
  END IF;

 ELSIF TG_TABLE_NAME='workflows_proassignment' THEN
  IF TG_OP='INSERT' THEN
   IF NEW.created_by_id IS DISTINCT FROM actor OR NEW.state<>'active'
    OR NOT (NEW.response_role=ANY(__RESPONSE_ROLES__))
    OR NOT clinic_app.has_permission('clinical.write',NEW.clinic_id,NEW.enrollment_id)
    OR (NEW.plan_id IS NOT NULL AND NOT EXISTS (SELECT 1
     FROM clinic_app.workflows_careplan p WHERE p.id=NEW.plan_id
     AND p.enrollment_id=NEW.enrollment_id))
   THEN RAISE EXCEPTION 'invalid pro assignment' USING ERRCODE='23514'; END IF;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['state','revision','last_command_key','last_command_digest',
    'updated_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','revision',
    'last_command_key','last_command_digest','updated_at'])
    OR OLD.state<>'active' OR NEW.state<>'completed'
    OR EXISTS (SELECT 1 FROM clinic_app.workflows_prooccurrence o
     WHERE o.assignment_id=NEW.id AND o.state='pending')
    OR NOT clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)
   THEN RAISE EXCEPTION 'invalid pro assignment transition'
    USING ERRCODE='23514'; END IF;
  END IF;

 ELSE
  SELECT * INTO v_asg FROM clinic_app.workflows_proassignment a
   WHERE a.id=NEW.assignment_id;
  IF v_asg.id IS NULL THEN
   RAISE EXCEPTION 'occurrence assignment is missing' USING ERRCODE='23514'; END IF;
  IF TG_OP='INSERT' THEN
   IF v_asg.state<>'active' OR NEW.created_by_id IS DISTINCT FROM actor
    OR NEW.created_by_id<>v_asg.created_by_id
    OR NEW.sequence NOT BETWEEN 1 AND v_asg.occurrences
    OR NEW.due_at<>v_asg.first_due_at+(NEW.sequence-1)*v_asg.interval_days
     *interval '1 day'
    OR NEW.state<>'pending' OR NEW.recorded_at IS NOT NULL
    OR NOT clinic_app.has_permission('clinical.write',NEW.clinic_id,v_asg.enrollment_id)
   THEN RAISE EXCEPTION 'invalid pro occurrence' USING ERRCODE='23514'; END IF;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['state','answers','concerning','response_task_id',
    'recorded_at','revision','last_command_key','last_command_digest','updated_at'])
    IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','answers','concerning',
    'response_task_id','recorded_at','revision','last_command_key',
    'last_command_digest','updated_at']) OR OLD.state<>'pending'
   THEN RAISE EXCEPTION 'pro occurrence identity is immutable'
    USING ERRCODE='23514'; END IF;
   IF NEW.state='responded' THEN
    -- A response is an observation of this patient: the nurse or clinician
    -- on the patient's care records it, never staff routing authority.
    IF NEW.recorded_at IS NULL OR NEW.answers IS NULL OR NEW.response_task_id IS NULL
     OR NOT (clinic_app.has_permission('observation.write',NEW.clinic_id,
      v_asg.enrollment_id) OR clinic_app.has_permission('clinical.write',
      NEW.clinic_id,v_asg.enrollment_id))
    THEN RAISE EXCEPTION 'pro response denied' USING ERRCODE='42501'; END IF;
   ELSIF NEW.state='no_response' THEN
    IF NEW.due_at+__GRACE__*interval '1 day'>now_ OR NOT
     clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)
    THEN RAISE EXCEPTION 'pro nonresponse denied' USING ERRCODE='23514'; END IF;
   ELSE RAISE EXCEPTION 'invalid pro occurrence transition' USING ERRCODE='23514';
   END IF;
  END IF;
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_care_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_care_guard() TO clinic_owner;

CREATE FUNCTION clinic_app.workflows_care_items(due_by timestamptz, requested uuid)
RETURNS TABLE(kind text, record_id uuid, organization_id uuid, clinic_id uuid,
 actor_id uuid, due timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT 'recall'::text,r.id,r.organization_id,r.clinic_id,r.created_by_id,r.due_at
  FROM clinic_app.workflows_recall r
  WHERE r.state='scheduled' AND r.due_at<=due_by
  AND (requested IS NULL OR r.id=requested)
 UNION ALL
 SELECT 'overdue',a.id,a.organization_id,a.clinic_id,a.created_by_id,a.window_end
  FROM clinic_app.workflows_careaction a
  WHERE a.state IN ('open','assigned') AND a.review_state<>'required'
  AND a.window_end<=due_by AND (requested IS NULL OR a.id=requested)
 UNION ALL
 SELECT 'pro_missed',o.id,o.organization_id,o.clinic_id,o.created_by_id,
  o.due_at+__GRACE__*interval '1 day'
  FROM clinic_app.workflows_prooccurrence o
  WHERE o.state='pending' AND o.due_at+__GRACE__*interval '1 day'<=due_by
  AND (requested IS NULL OR o.id=requested)
 UNION ALL
 SELECT 'stale',p.id,p.organization_id,p.clinic_id,p.clinician_id,p.created_at
  FROM clinic_app.workflows_careplan p
  WHERE (requested IS NULL OR p.id=requested)
  AND ((p.state='active' AND NOT EXISTS (SELECT 1
   FROM clinic_app.ehr_clinicaldocumentversion v
   WHERE v.id=p.source_version_id AND v.state='finalized'))
  OR (p.state='superseded' AND EXISTS (SELECT 1 FROM clinic_app.workflows_careaction a
   WHERE a.plan_id=p.id AND a.review_state='none'
   AND a.state IN ('open','assigned','overdue'))))
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_care_items(timestamptz,uuid) FROM PUBLIC;

CREATE FUNCTION clinic_app.workflows_care_due(due_by timestamptz)
RETURNS TABLE(kind text, record_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 WITH due AS (SELECT i.kind,i.record_id,i.organization_id,i.due
  FROM clinic_app.workflows_care_items(due_by,NULL) i
  WHERE NOT EXISTS (SELECT 1 FROM clinic_app.workflows_carerefusal x
   WHERE x.kind=i.kind AND x.record_id=i.record_id AND x.retry_at>due_by))
 SELECT d.kind,d.record_id FROM (SELECT DISTINCT due.organization_id FROM due) t
 CROSS JOIN LATERAL (SELECT due.kind,due.record_id,due.due FROM due
  WHERE due.organization_id=t.organization_id
  ORDER BY due.due,due.record_id LIMIT __PER_TENANT__) d
 ORDER BY d.due,d.record_id LIMIT __SCAN_LIMIT__
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_care_due(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_care_due(timestamptz) TO clinic_app;

CREATE FUNCTION clinic_app.workflows_care_scope(requested_kind text, requested uuid)
RETURNS TABLE(organization_id uuid, clinic_id uuid, actor_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT i.organization_id,i.clinic_id,i.actor_id
 FROM clinic_app.workflows_care_items(clinic_app.workflows_now(),requested) i
 WHERE i.kind=requested_kind
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_care_scope(text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_care_scope(text,uuid) TO clinic_app;

CREATE FUNCTION clinic_app.workflows_care_park(requested_kind text, requested uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 UPDATE clinic_app.workflows_carerefusal x SET attempts=x.attempts+1,
  actor_id=i.actor_id,refused_at=clinic_app.workflows_now(),
  retry_at=clinic_app.workflows_now()+__RETRY__*interval '1 minute'
 FROM clinic_app.workflows_care_items(clinic_app.workflows_now(),requested) i
 WHERE i.kind=requested_kind AND x.kind=i.kind AND x.record_id=i.record_id;
 INSERT INTO clinic_app.workflows_carerefusal(id,organization_id,clinic_id,kind,
  record_id,actor_id,attempts,refused_at,retry_at)
 SELECT gen_random_uuid(),i.organization_id,i.clinic_id,i.kind,i.record_id,
  i.actor_id,1,clinic_app.workflows_now(),
  clinic_app.workflows_now()+__RETRY__*interval '1 minute'
 FROM clinic_app.workflows_care_items(clinic_app.workflows_now(),requested) i
 WHERE i.kind=requested_kind AND NOT EXISTS (SELECT 1
  FROM clinic_app.workflows_carerefusal x
  WHERE x.kind=i.kind AND x.record_id=i.record_id);
 SELECT EXISTS (SELECT 1 FROM clinic_app.workflows_carerefusal x
  WHERE x.kind=requested_kind AND x.record_id=requested
  AND x.refused_at=clinic_app.workflows_now())
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_care_park(text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_care_park(text,uuid) TO clinic_app;

CREATE FUNCTION clinic_app.workflows_care_version_scope(clinic uuid,
 requested_version uuid)
RETURNS TABLE(encounter_id uuid, enrollment_id uuid, state text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT e.id,n.id,v.state::text FROM clinic_app.ehr_clinicaldocumentversion v
 JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
 JOIN clinic_app.ehr_encounter e ON e.id=d.encounter_id
 JOIN clinic_app.intake_patientclinicenrollment n ON n.patient_id=e.patient_id
  AND n.clinic_id=e.clinic_id
 WHERE v.id=requested_version AND e.clinic_id=clinic AND e.organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.has_permission('tasks.view',clinic,NULL)
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_care_version_scope(uuid,uuid)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_care_version_scope(uuid,uuid)
 TO clinic_app;

CREATE FUNCTION clinic_app.workflows_care_occurrence_scope(clinic uuid,
 requested_occurrence uuid)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT a.enrollment_id FROM clinic_app.workflows_prooccurrence o
 JOIN clinic_app.workflows_proassignment a ON a.id=o.assignment_id
 WHERE o.id=requested_occurrence AND o.clinic_id=clinic AND o.organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.has_permission('tasks.view',clinic,NULL)
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_care_occurrence_scope(uuid,uuid)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_care_occurrence_scope(uuid,uuid)
 TO clinic_app;

CREATE FUNCTION clinic_app.workflows_care_system_task_valid(row_data jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.workflows_prooccurrence o
 JOIN clinic_app.workflows_proassignment a ON a.id=o.assignment_id
 WHERE o.response_task_id=(row_data->>'id')::uuid AND o.state='responded'
 AND o.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND row_data->'due_at'=to_jsonb(o.recorded_at)
 AND (row_data-'id'-'due_at'-'created_at'-'updated_at')=jsonb_build_object(
  'organization_id',o.organization_id,'clinic_id',o.clinic_id,'kind','review',
  'subject_ref',jsonb_build_object('kind','enrollment','id',a.enrollment_id::text),
  'created_by_id',NULL,'owner_user_id',NULL,'owner_role',a.response_role,
  'priority',CASE WHEN o.concerning THEN 'urgent' ELSE 'normal' END,
  'state','assigned','escalation_policy_version',1,'escalated_at',NULL,
  'depends_on_id',NULL,'completion_evidence','{}'::jsonb,'revision',1,
  'idempotency_key',o.id,'fingerprint',encode(clinic_app.digest(
   'workflows-pro-review:'||o.id::text,'sha256'),'hex'),'last_command_key',NULL,
  'last_command_digest',''))
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_care_system_task_valid(jsonb) FROM PUBLIC;
__TASK_GUARD__

CREATE FUNCTION clinic_app.workflows_care_source_current(requested_action uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT COALESCE((SELECT a.review_state='confirmed' OR EXISTS (SELECT 1
   FROM clinic_app.ehr_clinicaldocumentversion v
   WHERE v.id=p.source_version_id AND v.state='finalized')
  FROM clinic_app.workflows_careaction a
  JOIN clinic_app.workflows_careplan p ON p.id=a.plan_id
  WHERE a.id=requested_action AND a.organization_id=NULLIF(
   current_setting('app.current_tenant',true),'')::uuid),false)
$f$;
REVOKE ALL ON FUNCTION clinic_app.workflows_care_source_current(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.workflows_care_source_current(uuid)
 TO clinic_app;

RESET ROLE;
""".replace("__ROLES__", _ROLES)
    .replace("__GRACE__", str(vocab.NO_RESPONSE_GRACE_DAYS))
    .replace("__PER_TENANT__", str(vocab.SCAN_PER_TENANT))
    .replace("__SCAN_LIMIT__", str(vocab.SCAN_LIMIT))
    .replace("__RETRY__", str(vocab.SCAN_RETRY_MINUTES))
    .replace("__RESPONSE_ROLES__", _RESPONSE_ROLES)
    .replace("__TASK_GUARD__", TASK_GUARD)
    .replace("__MIRRORS_A__", mirrors("a"))
    .replace("__MIRRORS_NEW__", mirrors("NEW"))
)

# Row visibility (RP matrix): staff see routing metadata; clinical content (an
# instruction, a referral destination, a PRO response) is read only by the
# patient's own clinicians, never by manager or organization-admin routing.
_PLAN_READER = (
    "EXISTS (SELECT 1 FROM clinic_app.workflows_careplan p WHERE p.id=plan_id AND "
    "clinic_app.has_permission('clinical.read',p.clinic_id,p.enrollment_id))"
)
_READ = {
    "workflows_careplan": (
        "(__VIEW__ AND (clinician_id=__ACTOR__ OR __MANAGE__)) OR "
        "clinic_app.has_permission('clinical.read',clinic_id,enrollment_id)"
    ),
    "workflows_careaction": (
        "(kind<>'patient_instruction' AND __VIEW__ AND (created_by_id=__ACTOR__ OR "
        "__MANAGE__ OR clinic_app.workflows_owned(clinic_id,owner_user_id,"
        "owner_role) OR EXISTS (SELECT 1 FROM clinic_app.workflows_careplan p "
        "WHERE p.id=plan_id AND p.clinician_id=__ACTOR__))) OR __PLAN_READER__"
    ),
    "workflows_referral": (
        "(target_label IS NULL AND __VIEW__ AND (created_by_id=__ACTOR__ OR "
        "__MANAGE__)) OR __PLAN_READER__ OR EXISTS (SELECT 1 "
        "FROM clinic_app.workflows_careaction a WHERE a.id=action_id AND "
        "clinic_app.workflows_owned(a.clinic_id,a.owner_user_id,a.owner_role))"
    ),
    "workflows_recall": "__VIEW__ AND (created_by_id=__ACTOR__ OR __MANAGE__)",
    "workflows_proassignment": (
        "clinic_app.has_permission('clinical.read',clinic_id,enrollment_id) OR "
        "clinic_app.has_permission('observation.write',clinic_id,enrollment_id)"
    ),
    "workflows_prooccurrence": (
        "EXISTS (SELECT 1 FROM clinic_app.workflows_proassignment a "
        "WHERE a.id=assignment_id)"
    ),
}
for _table in TABLES:
    _read = (
        _READ[_table]
        .replace("__PLAN_READER__", _PLAN_READER)
        .replace("__VIEW__", "clinic_app.has_permission('tasks.view',clinic_id,NULL)")
        .replace(
            "__MANAGE__", "clinic_app.has_permission('tasks.reassign',clinic_id,NULL)"
        )
        .replace("__ACTOR__", _ACTOR)
    )
    _sql += (
        """
ALTER TABLE clinic_app.__TABLE__ ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.__TABLE__ FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.__TABLE__ ADD CONSTRAINT __TABLE___clinic_fk
 FOREIGN KEY (organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
CREATE POLICY care_owner ON clinic_app.__TABLE__ TO clinic_owner
 USING (__SCOPE__) WITH CHECK (__SCOPE__);
CREATE POLICY care_read ON clinic_app.__TABLE__ FOR SELECT TO clinic_app
 USING (__SCOPE__ AND (__READ__));
CREATE POLICY care_insert ON clinic_app.__TABLE__ FOR INSERT TO clinic_app
 WITH CHECK (__SCOPE__);
CREATE POLICY care_update ON clinic_app.__TABLE__ FOR UPDATE TO clinic_app
 USING (__SCOPE__ AND (__READ__))
 WITH CHECK (__SCOPE__ AND clinic_app.has_permission('tasks.view',clinic_id,NULL));
CREATE TRIGGER care_guard BEFORE INSERT OR UPDATE OR DELETE ON clinic_app.__TABLE__
 FOR EACH ROW EXECUTE FUNCTION clinic_app.workflows_care_guard();
""".replace("__TABLE__", _table)
        .replace("__SCOPE__", _SCOPE)
        .replace("__READ__", _read)
    )

# Every reference between care rows, tasks and enrollments stays in one clinic.
_SCOPED_REFERENCES = (
    ("workflows_careplan", "enrollment", "intake_patientclinicenrollment"),
    ("workflows_careaction", "plan", "workflows_careplan"),
    ("workflows_careaction", "task", "workflows_task"),
    ("workflows_referral", "plan", "workflows_careplan"),
    ("workflows_referral", "action", "workflows_careaction"),
    ("workflows_recall", "enrollment", "intake_patientclinicenrollment"),
    ("workflows_recall", "task", "workflows_task"),
    ("workflows_proassignment", "enrollment", "intake_patientclinicenrollment"),
    ("workflows_proassignment", "plan", "workflows_careplan"),
    ("workflows_prooccurrence", "assignment", "workflows_proassignment"),
    ("workflows_prooccurrence", "response_task", "workflows_task"),
)
for _table, _column, _target in _SCOPED_REFERENCES:
    _sql += f"""
ALTER TABLE clinic_app.{_table} ADD CONSTRAINT {_table}_{_column}_scope_fk
 FOREIGN KEY (organization_id,clinic_id,{_column}_id)
 REFERENCES clinic_app.{_target}(organization_id,clinic_id,id)
 DEFERRABLE INITIALLY DEFERRED;
"""

# Scanner refusals: written by the park resolver only, read by clinic managers.
_sql += """
ALTER TABLE clinic_app.workflows_carerefusal ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.workflows_carerefusal FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.workflows_carerefusal ADD CONSTRAINT
 workflows_carerefusal_clinic_fk FOREIGN KEY (organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
CREATE POLICY care_owner ON clinic_app.workflows_carerefusal TO clinic_owner
 USING (__SCOPE__) WITH CHECK (__SCOPE__);
CREATE POLICY care_read ON clinic_app.workflows_carerefusal FOR SELECT TO clinic_app
 USING (__SCOPE__ AND clinic_app.has_permission('tasks.view',clinic_id,NULL)
 AND clinic_app.has_permission('tasks.reassign',clinic_id,NULL));
""".replace("__SCOPE__", _SCOPE)

_sql += """
CREATE TRIGGER care_review AFTER UPDATE ON clinic_app.workflows_prooccurrence
 FOR EACH ROW WHEN (OLD.state='pending' AND NEW.state='responded')
 EXECUTE FUNCTION clinic_app.workflows_care_guard();
CREATE TRIGGER care_task_gate BEFORE UPDATE ON clinic_app.workflows_task
 FOR EACH ROW WHEN (ROW(NEW.owner_user_id,NEW.owner_role) IS DISTINCT FROM
 ROW(OLD.owner_user_id,OLD.owner_role) OR (NEW.state IN ('assigned','in_progress',
 'done') AND NEW.state<>OLD.state))
 EXECUTE FUNCTION clinic_app.workflows_care_guard();
CREATE CONSTRAINT TRIGGER care_task_mirror AFTER UPDATE ON clinic_app.workflows_task
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN (ROW(NEW.owner_user_id,NEW.owner_role,NEW.state) IS DISTINCT FROM
 ROW(OLD.owner_user_id,OLD.owner_role,OLD.state))
 EXECUTE FUNCTION clinic_app.workflows_care_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.workflows_care_guard() FROM clinic_owner;
RESET ROLE;
"""

SQL: Final = _sql
