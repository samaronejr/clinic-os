"""AI-06 binds every revenue agent row to current Finance automation in SQL too."""

from apps.ai.migrations import _agent_guard_sql
from apps.ai.migrations._care_sql import _edit, _function, _resolver
from apps.ai.migrations._revenue_sql import _APPROVAL as PRIOR_APPROVAL
from apps.ai.migrations._revenue_sql import _CONTROLS as PRIOR_CONTROLS

# Identity-free ancestry: the caller's own visibility never decides whether a
# row belongs to AI-06, so a hidden revenue row cannot read as unrelated.
_ANCESTRY = r"""
CREATE FUNCTION clinic_app.ai_revenue_ancestry(clinic uuid,kind text,requested uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(
  SELECT 1 FROM clinic_app.ai_agentpolicyversion p
  WHERE p.clinic_id=clinic AND p.capability='revenue_cycle'
  AND p.organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND p.id=CASE kind
   WHEN 'policy' THEN requested
   WHEN 'run' THEN (SELECT r.policy_id FROM clinic_app.ai_agentrun r
    WHERE r.id=requested AND r.clinic_id=clinic)
   WHEN 'proposal' THEN (
    SELECT r.policy_id FROM clinic_app.ai_actionproposal a
    JOIN clinic_app.ai_agentrun r ON r.id=a.run_id
    WHERE a.id=requested AND a.clinic_id=clinic)
   WHEN 'execution' THEN (
    SELECT r.policy_id FROM clinic_app.ai_actionexecution e
    JOIN clinic_app.ai_actionproposal a ON a.id=e.proposal_id
    JOIN clinic_app.ai_agentrun r ON r.id=a.run_id
    WHERE e.id=requested AND e.clinic_id=clinic)
   ELSE NULL END)
$f$;
REVOKE ALL ON FUNCTION clinic_app.ai_revenue_ancestry(uuid,text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_revenue_ancestry(uuid,text,uuid)
 TO clinic_app,clinic_owner;
"""

# Revenue rows need the caller's Finance automation instead of agent staff.
_CONTROLS = _edit(
    PRIOR_CONTROLS,
    "ELSE (clinic_app.agent_staff(a.clinic_id) OR "
    "clinic_app.ai_revenue_row(a.clinic_id,'proposal',a.id)) END",
    "WHEN p.capability='revenue_cycle'\n"
    " THEN clinic_app.ai_revenue_row(a.clinic_id,'proposal',a.id)\n"
    " ELSE clinic_app.agent_staff(a.clinic_id) END",
)
# A revenue vote also stays current only while its approver holds it.
_APPROVAL = _edit(
    PRIOR_APPROVAL,
    "ELSE (clinic_app.agent_staff(a.clinic_id) OR "
    "clinic_app.ai_revenue_row(a.clinic_id,'proposal',a.proposal_id)) END",
    "WHEN clinic_app.ai_revenue_ancestry(a.clinic_id,'proposal',a.proposal_id)\n"
    " THEN clinic_app.ai_revenue_row(a.clinic_id,'proposal',a.proposal_id)\n"
    " AND coalesce(clinic_app.identity_actor_permission('automation.finance',\n"
    "  a.clinic_id,NULL,a.approver_id,a.organization_id),false)\n"
    " ELSE clinic_app.agent_staff(a.clinic_id) END",
)
# tiss.manage approves revenue proposals only, and only with Finance automation.
_PRIOR_GUARD = _function(_agent_guard_sql.SQL, "agent_record_guard")
_GUARD = _edit(
    _PRIOR_GUARD,
    "    NEW.clinic_id,NEW.authority_snapshot->>'permission') THEN\n"
    "   RAISE EXCEPTION 'agent approval refused'",
    "    NEW.clinic_id,NEW.authority_snapshot->>'permission')\n"
    "   OR (NEW.authority_snapshot->>'permission'='tiss.manage') IS DISTINCT FROM\n"
    "    clinic_app.ai_revenue_ancestry(NEW.clinic_id,'proposal',NEW.proposal_id)\n"
    "   OR (clinic_app.ai_revenue_ancestry(NEW.clinic_id,'proposal',NEW.proposal_id)\n"
    "    AND NOT clinic_app.has_permission('automation.finance',NEW.clinic_id,NULL))\n"
    "   THEN\n"
    "   RAISE EXCEPTION 'agent approval refused'",
)

_TABLES = (
    ("agentpolicyversion", "policy", "id"),
    ("agentrun", "policy", "policy_id"),
    ("actionproposal", "run", "run_id"),
    ("approval", "proposal", "proposal_id"),
    ("actionexecution", "proposal", "proposal_id"),
    ("actionreceipt", "execution", "execution_id"),
    ("compensation", "execution", "execution_id"),
)
_POLICIES = ""
_DROPS = ""
for _table, _kind, _column in _TABLES:
    # Restrictive: every clinic_app policy, agent_staff included, also needs it.
    _bound = (
        f"NOT clinic_app.ai_revenue_ancestry(clinic_id,'{_kind}',{_column})\n"
        f"  OR clinic_app.ai_revenue_row(clinic_id,'{_kind}',{_column})"
    )
    _POLICIES += f"""
CREATE POLICY agent_finance_bound ON clinic_app.ai_{_table} AS RESTRICTIVE
 TO clinic_app USING({_bound}) WITH CHECK({_bound});
"""
    _DROPS += f"DROP POLICY agent_finance_bound ON clinic_app.ai_{_table};\n"

SQL = _resolver((_ANCESTRY, _CONTROLS, _APPROVAL)) + _GUARD + "\n" + _POLICIES
REVERSE_SQL = (
    _DROPS
    + _PRIOR_GUARD
    + "\n"
    + _resolver(
        (
            PRIOR_CONTROLS,
            PRIOR_APPROVAL,
            "DROP FUNCTION clinic_app.ai_revenue_ancestry(uuid,text,uuid);",
        )
    )
)
