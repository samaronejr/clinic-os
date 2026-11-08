"""AI-06 widens only released vocabularies and the finance approval snapshot."""

from apps.ai.migrations import _agent_access_sql
from apps.ai.migrations._care_sql import _EVAL as PRIOR_EVAL
from apps.ai.migrations._care_sql import _STAFF as PRIOR_STAFF
from apps.ai.migrations._care_sql import _VALID as PRIOR_VALID
from apps.ai.migrations._care_sql import _edit, _function, _resolver

_STAFF = _edit(
    PRIOR_STAFF,
    " ELSE false END\n$f$;",
    " WHEN 'revenue_cycle' THEN\n"
    "  clinic_app.has_permission('charge.read',clinic,NULL)\n"
    "  AND clinic_app.has_permission('tiss.read',clinic,NULL)\n"
    "  AND clinic_app.has_permission('tiss.manage',clinic,NULL)\n"
    "  AND enrollment IS NULL\n"
    " ELSE false END\n$f$;",
)
_VALID = _edit(
    PRIOR_VALID,
    "'decision_support','care_coordination'])",
    "'decision_support','care_coordination','revenue_cycle'])",
    count=2,
)
_EVAL = _edit(
    PRIOR_EVAL,
    "WHEN 'care_coordination' THEN 'AI-05'",
    "WHEN 'care_coordination' THEN 'AI-05' WHEN 'revenue_cycle' THEN 'AI-06'",
)
_PRIOR_SNAPSHOT = _function(_agent_access_sql.SQL, "agent_snapshot")
_SNAPSHOT = _edit(
    _PRIOR_SNAPSHOT,
    "'automation.organization','tasks.assign')",
    "'automation.organization','tasks.assign','tiss.manage')",
)

_PRIOR_CONTROLS = _function(_agent_access_sql.SQL, "agent_controls")
_PRIOR_APPROVAL = _function(_agent_access_sql.SQL, "agent_approval_current")
_CONTROLS = _edit(
    _PRIOR_CONTROLS,
    "ELSE clinic_app.agent_staff(a.clinic_id) END",
    "ELSE (clinic_app.agent_staff(a.clinic_id) OR "
    "clinic_app.ai_revenue_row(a.clinic_id,'proposal',a.id)) END",
)
_APPROVAL = _edit(
    _PRIOR_APPROVAL,
    "ELSE clinic_app.agent_staff(a.clinic_id) END",
    "ELSE (clinic_app.agent_staff(a.clinic_id) OR "
    "clinic_app.ai_revenue_row(a.clinic_id,'proposal',a.proposal_id)) END",
)

_ROW = r"""
CREATE FUNCTION clinic_app.ai_revenue_row(clinic uuid,kind text,requested uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.ai_staff(clinic,'revenue_cycle',NULL)
 AND clinic_app.has_permission('automation.finance',clinic,NULL)
 AND EXISTS(
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
REVOKE ALL ON FUNCTION clinic_app.ai_revenue_row(uuid,text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_revenue_row(uuid,text,uuid)
 TO clinic_app,clinic_owner;
"""

SQL = _resolver((_STAFF, _VALID, _EVAL, _SNAPSHOT, _ROW, _CONTROLS, _APPROVAL))
REVERSE_SQL = _resolver(
    (
        _PRIOR_CONTROLS,
        _PRIOR_APPROVAL,
        PRIOR_STAFF,
        PRIOR_VALID,
        PRIOR_EVAL,
        _PRIOR_SNAPSHOT,
    )
)
for _table, _kind, _column in (
    ("agentpolicyversion", "policy", "id"),
    ("agentrun", "policy", "policy_id"),
    ("actionproposal", "run", "run_id"),
    ("approval", "proposal", "proposal_id"),
    ("actionexecution", "proposal", "proposal_id"),
    ("actionreceipt", "execution", "execution_id"),
    ("compensation", "execution", "execution_id"),
):
    SQL += f"""
CREATE POLICY agent_finance ON clinic_app.ai_{_table} TO clinic_app
 USING(organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND clinic_app.ai_revenue_row(clinic_id,'{_kind}',{_column}))
 WITH CHECK(organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND clinic_app.ai_revenue_row(clinic_id,'{_kind}',{_column}));
"""
    REVERSE_SQL += f"DROP POLICY agent_finance ON clinic_app.ai_{_table};\n"
REVERSE_SQL += """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ai_revenue_row(uuid,text,uuid);
RESET ROLE;
"""
