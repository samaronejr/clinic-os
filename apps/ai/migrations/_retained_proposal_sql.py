"""Authenticate the encrypted proposal's actual retained source before validation."""

from apps.ai.migrations import _approval_attestation_sql, _retained_effect_sql
from apps.ai.migrations._care_sql import _edit, _resolver
from apps.ai.migrations._front_desk_authority_sql import _function

_ROLLOUT_PRIOR = _function(_retained_effect_sql.SQL, "ai_rollout_guard")
_ROLLOUT_REJECTION = _edit(
    _ROLLOUT_PRIOR,
    "  IF EXISTS(SELECT 1 FROM clinic_app.ai_agentpolicyversion p",
    "  IF (TG_OP='INSERT' OR NEW.outcome<>'rejected')\n"
    "  AND EXISTS(SELECT 1 FROM clinic_app.ai_agentpolicyversion p",
)
_ROLLOUT_CAPABILITIES = _edit(
    _ROLLOUT_REJECTION,
    "p.capability='care_coordination'",
    "p.capability IN ('care_coordination','revenue_cycle')",
)
_ROLLOUT = _edit(
    _ROLLOUT_CAPABILITIES,
    "source.purpose='care_coordination'",
    "source.purpose=(SELECT capability FROM clinic_app.ai_agentpolicyversion"
    " WHERE id=NEW.policy_id)",
)
_AGENT_PRIOR = _function(_retained_effect_sql.SQL, "ai_rollout_agent_allowed")
_AGENT = _edit(
    _AGENT_PRIOR,
    "p.capability<>'care_coordination'",
    "p.capability NOT IN ('care_coordination','revenue_cycle')",
)
_RECORD_PRIOR = _function(_approval_attestation_sql.SQL, "agent_record_guard")
_RECORD = _edit(
    _RECORD_PRIOR,
    " IF TG_OP='UPDATE' THEN",
    """ IF TG_OP='UPDATE' AND TG_TABLE_NAME='ai_actionproposal' THEN
  IF NEW.source_xid IS DISTINCT FROM OLD.source_xid
  AND (to_jsonb(NEW)-'source_xid') IS NOT DISTINCT FROM
      (to_jsonb(OLD)-'source_xid') THEN RETURN NEW; END IF;
 END IF;
 IF TG_OP='UPDATE' THEN""",
)

_AUTHORIZE = r"""
CREATE FUNCTION clinic_app.ai_authorize_retained_proposal(
 requested uuid,clinic uuid,kek text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE a clinic_app.ai_actionproposal; r clinic_app.ai_agentrun;
 retained_capability text; raw bytea; data jsonb; origin uuid;
BEGIN
 SELECT * INTO a FROM clinic_app.ai_actionproposal WHERE id=requested;
 SELECT * INTO r FROM clinic_app.ai_agentrun WHERE id=a.run_id;
 SELECT capability INTO retained_capability FROM clinic_app.ai_agentpolicyversion
 WHERE id=r.policy_id;
 IF a.id IS NULL OR a.clinic_id IS DISTINCT FROM clinic
 OR a.organization_id IS DISTINCT FROM
  NULLIF(current_setting('app.current_tenant',true),'')::uuid
 OR retained_capability NOT IN ('care_coordination','revenue_cycle')
 OR NOT clinic_app.ai_rollout_agent_allowed(r.id)
 OR NOT clinic_app.ai_effect_current(r.invocation_id,clinic)
 OR NOT (CASE WHEN retained_capability='revenue_cycle'
  THEN clinic_app.ai_revenue_row(clinic,'proposal',a.id)
  ELSE clinic_app.agent_staff(clinic) OR clinic_app.ai_care_staff(clinic) END)
 THEN RETURN false; END IF;
 raw:=clinic_app.protected_decrypt(kek,'ai.action_proposal.payload.v1',a.payload);
 data:=convert_from(raw,'UTF8')::jsonb;
 IF encode(clinic_app.digest(raw,'sha256'),'hex') IS DISTINCT FROM a.digest
 OR data->>'action_id' IS DISTINCT FROM a.id::text
 OR data->>'clinic_id' IS DISTINCT FROM clinic::text
 OR data->>'organization_id' IS DISTINCT FROM a.organization_id::text
 OR data->>'idempotency_key' IS DISTINCT FROM a.idempotency_key::text
 OR jsonb_typeof(data->'source_versions') IS DISTINCT FROM 'array'
 THEN RETURN false; END IF;
 IF retained_capability='revenue_cycle' THEN
  SELECT output.invocation_id INTO origin FROM clinic_app.ai_aiartifact output
  WHERE output.id::text=data->'subject'->>'id'
  AND output.clinic_id=clinic AND output.organization_id=a.organization_id
  AND output.purpose=retained_capability AND output.kind='output'
  AND output.purged_at IS NULL AND output.expires_at>statement_timestamp()
  AND data->'subject'=jsonb_build_object('kind','ai_artifact','id',output.id::text,
   'expected_state','output','revision',1)
  AND data->'source_versions'=jsonb_build_array(jsonb_build_object(
   'kind','ai_artifact','id',output.id::text,'version',1,'revision',1,
   'content_sha256',encode(clinic_app.digest(clinic_app.protected_decrypt(
    kek,'ai.artifact.content.v1',output.content),'sha256'),'hex')));
 ELSE
  SELECT item.invocation_id INTO origin FROM clinic_app.ai_careproposal item
  CROSS JOIN jsonb_array_elements(data->'source_versions') cited
  WHERE cited->>'kind'='care_proposal' AND item.id::text=cited->>'id'
  AND item.clinic_id=clinic AND item.organization_id=a.organization_id
  AND item.plan_id::text=data->'subject'->>'id'
  AND data->'subject'->>'kind'='care_plan'
  AND item.audience='administrative' AND item.state='pending';
 END IF;
 IF origin IS DISTINCT FROM r.invocation_id OR origin IS NULL
 OR NOT clinic_app.ai_effect_current(origin,clinic) THEN RETURN false; END IF;
 UPDATE clinic_app.ai_actionproposal SET source_xid=pg_current_xact_id()::text
 WHERE id=a.id AND source_xid IS DISTINCT FROM pg_current_xact_id()::text;
 RETURN true;
END $f$;
"""

_GUARD = r"""
CREATE FUNCTION clinic_app.ai_retained_proposal_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE r clinic_app.ai_agentrun; retained_capability text;
BEGIN
 IF TG_OP='INSERT' AND NEW.source_xid<>'' THEN
  RAISE EXCEPTION 'proposal source receipt is private' USING ERRCODE='42501';
 END IF;
 SELECT * INTO r FROM clinic_app.ai_agentrun WHERE id=NEW.run_id;
 SELECT capability INTO retained_capability FROM clinic_app.ai_agentpolicyversion
 WHERE id=r.policy_id;
 IF retained_capability IN ('care_coordination','revenue_cycle')
 AND NEW.state NOT IN ('proposal','rejected')
 AND (NEW.source_xid IS DISTINCT FROM pg_current_xact_id()::text
  OR NOT clinic_app.ai_effect_current(r.invocation_id,NEW.clinic_id)) THEN
  RAISE EXCEPTION 'original proposal source required' USING ERRCODE='42501';
 END IF;
 RETURN NEW;
END $f$;
"""

_INSTALL = """
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.ai_authorize_retained_proposal(uuid,uuid,text),
 clinic_app.ai_retained_proposal_guard() FROM PUBLIC,clinic_app,clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.ai_authorize_retained_proposal(uuid,uuid,text)
 TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.ai_retained_proposal_guard() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER ai_retained_proposal_guard BEFORE INSERT OR UPDATE
 ON clinic_app.ai_actionproposal FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_retained_proposal_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ai_retained_proposal_guard() FROM clinic_owner;
RESET ROLE;
"""
SQL = "".join(
    (
        "GRANT UPDATE(source_xid) ON clinic_app.ai_actionproposal "
        "TO clinic_resolver;\n",
        _resolver((_ROLLOUT, _AGENT, _AUTHORIZE, _GUARD)),
        _RECORD,
        _INSTALL,
    )
)

_REVERSE_START = """
DROP TRIGGER ai_retained_proposal_guard ON clinic_app.ai_actionproposal;
SET LOCAL ROLE clinic_resolver;
DO $rollback$
BEGIN
 IF EXISTS(SELECT 1 FROM clinic_app.ai_actionproposal WHERE source_xid<>'')
 THEN RAISE EXCEPTION 'proposal source evidence requires restore'
 USING ERRCODE='23514'; END IF;
END $rollback$;
DROP FUNCTION clinic_app.ai_retained_proposal_guard();
DROP FUNCTION clinic_app.ai_authorize_retained_proposal(uuid,uuid,text);
RESET ROLE;
"""
REVERSE_SQL = "".join(
    (
        _REVERSE_START,
        _resolver((_ROLLOUT_PRIOR, _AGENT_PRIOR)),
        _RECORD_PRIOR,
        "REVOKE UPDATE(source_xid) ON clinic_app.ai_actionproposal "
        "FROM clinic_resolver;\n",
    )
)
