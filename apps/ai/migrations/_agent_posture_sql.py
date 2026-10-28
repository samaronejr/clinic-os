"""Frozen FORCE-RLS, composite foreign keys, and exact grants for all seven tables."""

_TABLES = (
    "agentpolicyversion",
    "agentrun",
    "actionproposal",
    "approval",
    "actionexecution",
    "actionreceipt",
    "compensation",
)
_PARENTS = {
    "agentrun": ("policy", "agentpolicyversion"),
    "actionproposal": ("run", "agentrun"),
    "approval": ("proposal", "actionproposal"),
    "actionexecution": ("proposal", "actionproposal"),
    "actionreceipt": ("execution", "actionexecution"),
    "compensation": ("execution", "actionexecution"),
}
_MACHINE = {
    "agentpolicyversion": "principal_id",
    "agentrun": "principal_id",
    "actionproposal": """(SELECT r.principal_id FROM clinic_app.ai_agentrun r
 WHERE r.id=run_id)""",
    "approval": """(SELECT r.principal_id FROM clinic_app.ai_actionproposal p
 JOIN clinic_app.ai_agentrun r ON r.id=p.run_id WHERE p.id=proposal_id)""",
    "actionexecution": """(SELECT r.principal_id FROM clinic_app.ai_actionproposal p
 JOIN clinic_app.ai_agentrun r ON r.id=p.run_id WHERE p.id=proposal_id)""",
    "actionreceipt": """(SELECT r.principal_id FROM clinic_app.ai_actionexecution e
 JOIN clinic_app.ai_actionproposal p ON p.id=e.proposal_id
 JOIN clinic_app.ai_agentrun r ON r.id=p.run_id WHERE e.id=execution_id)""",
    "compensation": """(SELECT r.principal_id FROM clinic_app.ai_actionexecution e
 JOIN clinic_app.ai_actionproposal p ON p.id=e.proposal_id
 JOIN clinic_app.ai_agentrun r ON r.id=p.run_id WHERE e.id=execution_id)""",
}
SQL = ""
for _name in _TABLES:
    _table = f"clinic_app.ai_{_name}"
    _tenant = (
        "organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid"
    )
    _staff = f"{_tenant} AND clinic_app.agent_staff(clinic_id)"
    _principal = (
        "clinic_app.principal_has('agent.execute',clinic_id)"
        f" AND clinic_app.principal_scope({_MACHINE[_name]},clinic_id)"
        "=organization_id"
    )
    SQL += f"""
ALTER TABLE {_table} ADD CONSTRAINT ai_{_name}_clinic_fk
 FOREIGN KEY(organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
REVOKE ALL ON {_table} FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT,INSERT ON {_table} TO clinic_app;
GRANT SELECT ON {_table} TO clinic_agent;
ALTER TABLE {_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {_table} FORCE ROW LEVEL SECURITY;
CREATE POLICY agent_owner ON {_table} TO clinic_owner
 USING({_tenant}) WITH CHECK({_tenant});
CREATE POLICY agent_staff ON {_table} TO clinic_app
 USING({_staff}) WITH CHECK({_staff});
CREATE POLICY agent_machine ON {_table} TO clinic_agent
 USING({_principal}) WITH CHECK({_principal});
"""
    if _name in _PARENTS:
        _column, _parent = _PARENTS[_name]
        SQL += f"""
ALTER TABLE {_table} ADD CONSTRAINT ai_{_name}_parent_fk
 FOREIGN KEY(organization_id,clinic_id,{_column}_id)
 REFERENCES clinic_app.ai_{_parent}(organization_id,clinic_id,id);
"""
    if _name in {"agentrun", "actionproposal", "actionexecution", "compensation"}:
        SQL += f"GRANT UPDATE ON {_table} TO clinic_app,clinic_agent;\n"
    if _name in {"actionexecution", "actionreceipt", "compensation"}:
        SQL += f"GRANT INSERT ON {_table} TO clinic_agent;\n"
