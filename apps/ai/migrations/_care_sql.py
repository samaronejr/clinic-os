"""Care-coordination purpose (AI-05) and the exact care proposal posture.

The purpose joins the existing clinical purposes: patient-scoped
``clinical.read`` for artifacts and the organization opt-in vocabulary. The
replaced resolver bodies are derived from the released text with anchored edits,
so every other term stays byte-for-byte as deployed.
"""

from __future__ import annotations

from apps.ai.migrations import _evaluation_sql, _policy_sql
from apps.orders.migrations._access_sql import render

TABLE = "ai_careproposal"
ADMIN_KINDS = "'return_window','staff_coordination'"
PROPOSABLE_KINDS = ADMIN_KINDS + ",'order','questionnaire','monitoring_task'"


def _function(sql: str, name: str) -> str:
    """Return one released CREATE FUNCTION statement as CREATE OR REPLACE.

    Plain index and slice only, so the clock census decodes it statically.
    """
    head = f"CREATE FUNCTION clinic_app.{name}("
    if len(sql.split(head)) - 1 != 1:  # pragma: no cover - import-time contract
        message = f"released function {name} moved"
        raise RuntimeError(message)
    start = sql.index(head)
    end = sql.index("$f$;", start) + len("$f$;")
    return sql[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def _edit(text: str, anchor: str, replacement: str, count: int = 1) -> str:
    if len(text.split(anchor)) - 1 != count:  # pragma: no cover - import-time contract
        message = "released anchor moved"
        raise RuntimeError(message)
    return text.replace(anchor, replacement)


def _resolver(functions: tuple[str, ...]) -> str:
    return (
        "SET LOCAL ROLE clinic_resolver;\n" + "\n".join(functions) + "\nRESET ROLE;\n"
    )


_PRIOR_STAFF = _function(_policy_sql.SQL, "ai_staff")
_PRIOR_VALID = _function(_policy_sql.SQL, "ai_policy_valid")
_PRIOR_EVAL = _function(_evaluation_sql.SQL, "ai_eval_record_guard")

_STAFF = _edit(
    _PRIOR_STAFF,
    " ELSE false END\n$f$;",
    " WHEN 'care_coordination' THEN\n"
    "  clinic_app.has_permission('clinical.read',clinic,enrollment)\n"
    " ELSE false END\n$f$;",
)
_VALID = _edit(
    _PRIOR_VALID,
    "'front_desk','analyst','decision_support'])",
    "'front_desk','analyst','decision_support','care_coordination'])",
    count=2,
)
_EVAL = _edit(
    _PRIOR_EVAL,
    "WHEN 'front_desk' THEN 'AI-04'",
    "WHEN 'front_desk' THEN 'AI-04' WHEN 'care_coordination' THEN 'AI-05'",
)

PURPOSE_SQL = _resolver((_STAFF, _VALID, _EVAL))
PURPOSE_REVERSE_SQL = _resolver((_PRIOR_STAFF, _PRIOR_VALID, _PRIOR_EVAL))

ACCESS = r"""
GRANT SELECT ON clinic_app.ai_careproposal TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_care_staff(clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.agent_staff(clinic)
 AND clinic_app.has_permission('tasks.assign',clinic,NULL)
$f$;
CREATE FUNCTION clinic_app.ai_care_visible(
 clinic uuid,enrollment uuid,audience text,state text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('clinical.write',clinic,enrollment)
 OR (audience='administrative' AND state<>'refused'
  AND clinic_app.ai_care_staff(clinic))
$f$;
CREATE FUNCTION clinic_app.ai_care_plan_current(
 requested_plan uuid,requested_revision integer)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.workflows_careplan p
 JOIN clinic_app.ehr_clinicaldocumentversion v ON v.id=p.source_version_id
 WHERE p.id=requested_plan AND p.organization_id=__TENANT__
 AND p.state='active' AND p.revision=requested_revision AND v.state='finalized')
$f$;
CREATE FUNCTION clinic_app.ai_care_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE i clinic_app.ai_aiinvocation; a clinic_app.ai_aiartifact;
 p clinic_app.workflows_careplan; c clinic_app.workflows_careaction;
 r clinic_app.ai_actionproposal;
 allowed text[]:=ARRAY['state','reason_code','action_proposal_id',
  'care_action_id','decided_by_id','decided_at','revision'];
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'care proposal history retained' USING ERRCODE='23514'; END IF;
 IF NEW.organization_id IS DISTINCT FROM __TENANT__ THEN
  RAISE EXCEPTION 'care proposal denied' USING ERRCODE='42501'; END IF;
 IF TG_OP='INSERT' THEN
  PERFORM pg_advisory_xact_lock(hashtextextended('ai-care:'||NEW.invocation_id,0));
  SELECT * INTO i FROM clinic_app.ai_aiinvocation WHERE id=NEW.invocation_id;
  SELECT * INTO a FROM clinic_app.ai_aiartifact WHERE id=i.inputs_ref_id;
  SELECT * INTO p FROM clinic_app.workflows_careplan WHERE id=NEW.plan_id;
  IF NEW.requested_by_id IS DISTINCT FROM __ACTOR__
  OR (i.organization_id,i.clinic_id,i.user_id) IS DISTINCT FROM
   (NEW.organization_id,NEW.clinic_id,NEW.requested_by_id)
  OR i.purpose<>'care_coordination' OR i.status<>'succeeded'
  OR a.purpose<>'care_coordination'
  OR a.patient_enrollment_id IS DISTINCT FROM NEW.enrollment_id
  OR (p.organization_id,p.clinic_id,p.enrollment_id,p.source_version_id,p.revision)
   IS DISTINCT FROM (NEW.organization_id,NEW.clinic_id,NEW.enrollment_id,
   NEW.source_version_id,NEW.plan_revision)
  OR NOT clinic_app.ai_care_plan_current(NEW.plan_id,NEW.plan_revision)
  OR NOT clinic_app.has_permission('clinical.write',NEW.clinic_id,NEW.enrollment_id)
  THEN RAISE EXCEPTION 'care proposal denied' USING ERRCODE='42501'; END IF;
  IF NEW.state NOT IN ('pending','refused') OR NEW.revision<>1
  OR (NEW.kind IN (__ADMIN__) AND NEW.audience<>'administrative')
  OR (NEW.kind NOT IN (__ADMIN__) AND NEW.audience<>'clinical')
  OR (NEW.state='pending' AND NEW.kind NOT IN (__PROPOSABLE__))
  OR NEW.position NOT BETWEEN 0 AND 127
  OR NEW.position<>(SELECT count(*) FROM clinic_app.ai_careproposal x
   WHERE x.invocation_id=NEW.invocation_id)
  THEN RAISE EXCEPTION 'care proposal refused' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 SELECT * INTO p FROM clinic_app.workflows_careplan WHERE id=NEW.plan_id;
 IF (to_jsonb(NEW)-allowed) IS DISTINCT FROM (to_jsonb(OLD)-allowed)
 OR OLD.state<>'pending' OR NEW.revision<>OLD.revision+1
 OR NEW.decided_by_id IS DISTINCT FROM __ACTOR__
 THEN RAISE EXCEPTION 'care decision refused' USING ERRCODE='23514'; END IF;
 NEW.decided_at:=statement_timestamp();
 IF NEW.state IN ('approved','accepted') THEN
  SELECT * INTO c FROM clinic_app.workflows_careaction WHERE id=NEW.care_action_id;
  IF (c.organization_id,c.clinic_id,c.plan_id,c.kind,c.window_start,c.window_end)
   IS DISTINCT FROM (NEW.organization_id,NEW.clinic_id,NEW.plan_id,NEW.kind,
   NEW.window_start,NEW.window_end)
  OR NOT clinic_app.ai_care_plan_current(NEW.plan_id,NEW.plan_revision)
  THEN RAISE EXCEPTION 'care decision refused' USING ERRCODE='23514'; END IF;
 END IF;
 IF NEW.state='approved' THEN
  SELECT * INTO r FROM clinic_app.ai_actionproposal WHERE id=NEW.action_proposal_id;
  IF NEW.audience<>'administrative' OR NOT clinic_app.ai_care_staff(NEW.clinic_id)
  OR (r.organization_id,r.clinic_id,r.state) IS DISTINCT FROM
   (NEW.organization_id,NEW.clinic_id,'receipt')
  THEN RAISE EXCEPTION 'care approval refused' USING ERRCODE='42501'; END IF;
 ELSIF NEW.state='accepted' THEN
  IF NEW.audience<>'clinical' OR NOT clinic_app.has_permission(
   'clinical.write',NEW.clinic_id,NEW.enrollment_id)
  THEN RAISE EXCEPTION 'care acceptance refused' USING ERRCODE='42501'; END IF;
 ELSIF NEW.state='rejected' THEN
  IF NOT (clinic_app.has_permission('clinical.write',NEW.clinic_id,NEW.enrollment_id)
   OR (NEW.audience='administrative' AND clinic_app.ai_care_staff(NEW.clinic_id)))
  THEN RAISE EXCEPTION 'care rejection refused' USING ERRCODE='42501'; END IF;
 ELSIF NEW.state='stale' THEN
  IF NEW.reason_code<>'source_superseded'
  OR clinic_app.ai_care_plan_current(NEW.plan_id,NEW.plan_revision)
  THEN RAISE EXCEPTION 'care staleness refused' USING ERRCODE='23514'; END IF;
 ELSE
  RAISE EXCEPTION 'care decision refused' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_care_staff(uuid),
 clinic_app.ai_care_visible(uuid,uuid,text,text),
 clinic_app.ai_care_plan_current(uuid,integer),clinic_app.ai_care_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_care_staff(uuid),
 clinic_app.ai_care_visible(uuid,uuid,text,text) TO clinic_app,clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.ai_care_plan_current(uuid,integer)
 FROM clinic_app,clinic_owner,clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.ai_care_guard() TO clinic_owner;
RESET ROLE;
"""

POSTURE = r"""
ALTER TABLE clinic_app.ai_careproposal ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.ai_careproposal FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.ai_careproposal FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT,INSERT ON clinic_app.ai_careproposal TO clinic_app;
GRANT UPDATE(state,reason_code,action_proposal_id,care_action_id,decided_by_id,
 decided_at,revision) ON clinic_app.ai_careproposal TO clinic_app;
CREATE POLICY care_owner ON clinic_app.ai_careproposal TO clinic_owner
 USING(organization_id=__TENANT__) WITH CHECK(organization_id=__TENANT__);
CREATE POLICY care_staff ON clinic_app.ai_careproposal TO clinic_app
 USING(organization_id=__TENANT__
  AND clinic_app.ai_care_visible(clinic_id,enrollment_id,audience,state))
 WITH CHECK(organization_id=__TENANT__
  AND clinic_app.ai_care_visible(clinic_id,enrollment_id,audience,state));
CREATE TRIGGER care_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.ai_careproposal FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_care_guard();
CREATE TRIGGER care_no_truncate BEFORE TRUNCATE ON clinic_app.ai_careproposal
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.ai_care_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ai_care_guard() FROM clinic_owner;
RESET ROLE;
ALTER TABLE clinic_app.ai_careproposal ADD CONSTRAINT ai_care_clinic_fk
 FOREIGN KEY(organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
ALTER TABLE clinic_app.ai_careproposal ADD CONSTRAINT ai_care_invocation_scope
 FOREIGN KEY(organization_id,clinic_id,invocation_id)
 REFERENCES clinic_app.ai_aiinvocation(organization_id,clinic_id,id);
ALTER TABLE clinic_app.ai_careproposal ADD CONSTRAINT ai_care_plan_scope
 FOREIGN KEY(organization_id,clinic_id,plan_id)
 REFERENCES clinic_app.workflows_careplan(organization_id,clinic_id,id);
ALTER TABLE clinic_app.ai_careproposal ADD CONSTRAINT ai_care_enrollment_scope
 FOREIGN KEY(organization_id,clinic_id,enrollment_id)
 REFERENCES clinic_app.intake_patientclinicenrollment(organization_id,clinic_id,id);
ALTER TABLE clinic_app.ai_careproposal ADD CONSTRAINT ai_care_action_proposal_scope
 FOREIGN KEY(organization_id,clinic_id,action_proposal_id)
 REFERENCES clinic_app.ai_actionproposal(organization_id,clinic_id,id);
ALTER TABLE clinic_app.ai_careproposal ADD CONSTRAINT ai_care_action_scope
 FOREIGN KEY(organization_id,clinic_id,care_action_id)
 REFERENCES clinic_app.workflows_careaction(organization_id,clinic_id,id);
"""

SQL = render(
    ACCESS.replace("__ADMIN__", ADMIN_KINDS).replace("__PROPOSABLE__", PROPOSABLE_KINDS)
    + POSTURE
)

# Rehearsal only on an empty synthetic database; with history, rollback = restore.
REVERSE_SQL = r"""
DROP POLICY care_staff ON clinic_app.ai_careproposal;
DROP POLICY care_owner ON clinic_app.ai_careproposal;
DROP TRIGGER care_guard ON clinic_app.ai_careproposal;
DROP TRIGGER care_no_truncate ON clinic_app.ai_careproposal;
REVOKE SELECT ON clinic_app.ai_careproposal FROM clinic_resolver;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ai_care_guard();
DROP FUNCTION clinic_app.ai_care_visible(uuid,uuid,text,text);
DROP FUNCTION clinic_app.ai_care_staff(uuid);
DROP FUNCTION clinic_app.ai_care_plan_current(uuid,integer);
RESET ROLE;
"""
