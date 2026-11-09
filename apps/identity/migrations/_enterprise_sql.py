"""Install authority, exact ACLs, FORCE RLS and no-delete guards atomically.

The reverse is the unused rollback only: it refuses while any enterprise row
exists (populated authority history rolls back by restore), then restores the
prior audit append and workflow guard, strips the policy fence and removes
every object and grant this migration added, before Django drops the tables.
"""

from apps.identity.enterprise_contract import COLUMN_GRANTS, RUNTIME_GRANTS, TABLES
from apps.tenancy.migrations_support import revoke_default_dml

from ._enterprise_access_sql import SQL as ACCESS_SQL
from ._enterprise_audit_sql import PRIOR_SQL as PRIOR_AUDIT_SQL
from ._enterprise_audit_sql import SQL as AUDIT_SQL
from ._enterprise_context_fence_sql import REVERSE_SQL as CONTEXT_UNFENCE_SQL
from ._enterprise_context_fence_sql import SQL as CONTEXT_FENCE_SQL
from ._enterprise_directory_sql import SQL as DIRECTORY_SQL
from ._enterprise_guard_sql import SQL as GUARD_SQL
from ._enterprise_review_sql import SQL as REVIEW_SQL
from ._enterprise_unit_policy_sql import SQL as UNIT_POLICY_SQL
from ._workflow_review_source import PRIOR_GUARD_SQL

SIGNATURES = {
    "enterprise_org_admin(uuid)": True,
    "enterprise_directory_authorized(uuid)": True,
    "enterprise_scim_auth(bytea)": True,
    "enterprise_connection_scope(uuid)": True,
    "enterprise_scim_audit_scope(uuid)": True,
    "enterprise_new_directory_user(uuid)": True,
    "enterprise_sync_directory(uuid)": True,
    "enterprise_sso_account(uuid,bytea)": True,
    "enterprise_sso_principal(uuid,bytea)": True,
    "enterprise_manager(uuid)": True,
    "enterprise_break_glass_usable(uuid,uuid)": True,
    "enterprise_break_glass_versions(uuid)": True,
    "enterprise_break_glass_observations(uuid)": True,
    "enterprise_patient_access_log(uuid,integer)": True,
    "enterprise_support_scope(uuid,uuid,bytea)": True,
    "enterprise_unit_member_revoke(uuid,uuid)": True,
    "enterprise_template_propagate(uuid,uuid)": True,
    "enterprise_governance_guard()": False,
    "enterprise_break_glass_complete()": False,
    "enterprise_security_task(jsonb,text)": False,
    "enterprise_create_review(uuid)": True,
    "enterprise_finish_review(uuid)": True,
    "enterprise_unit_denied(uuid,uuid)": False,
    "enterprise_unit_membership_guard()": False,
    "enterprise_unit_policy_apply()": False,
}
OWNER_FUNCTIONS = frozenset(
    {
        "enterprise_new_directory_user(uuid)",
        "enterprise_sync_directory(uuid)",
        "enterprise_unit_member_revoke(uuid,uuid)",
        "enterprise_template_propagate(uuid,uuid)",
        "enterprise_unit_policy_apply()",
    }
)
_sql = ""
for _table in TABLES:
    _sql += revoke_default_dml(_table, keep=RUNTIME_GRANTS[_table])
    _sql += f"""
REVOKE ALL ON clinic_app.{_table} FROM PUBLIC,clinic_app,clinic_agent;
GRANT {",".join(sorted(RUNTIME_GRANTS[_table]))} ON clinic_app.{_table} TO clinic_app;
GRANT SELECT ON clinic_app.{_table} TO clinic_resolver;
ALTER TABLE clinic_app.{_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{_table} FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{_table} ADD CONSTRAINT {_table}_unit_fk
 FOREIGN KEY(organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
CREATE POLICY enterprise_owner ON clinic_app.{_table} TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
 NULLIF(current_setting('app.current_tenant',true),'')::uuid);
"""
for _table, _column, _privilege in sorted(COLUMN_GRANTS):
    _sql += f"GRANT {_privilege}({_column}) ON clinic_app.{_table} TO clinic_app;\n"
_sql += """
GRANT UPDATE(window_start,window_count) ON clinic_app.identity_scimtoken
 TO clinic_resolver;
GRANT INSERT ON clinic_app.workflows_task TO clinic_resolver;
GRANT UPDATE(owner_user_id,owner_role,state,revision,last_command_key,
 last_command_digest,completion_evidence,updated_at)
 ON clinic_app.workflows_task TO clinic_resolver;
GRANT UPDATE(review_task_id,reviewed_at,reviewed_by_id)
 ON clinic_app.identity_breakglassgrant TO clinic_resolver;
"""
_sql += DIRECTORY_SQL + ACCESS_SQL + GUARD_SQL + UNIT_POLICY_SQL + REVIEW_SQL
_function_acl = ""
for _signature, _public in SIGNATURES.items():
    _owner = "clinic_owner" if _signature in OWNER_FUNCTIONS else "clinic_resolver"
    _function_acl += f"""
ALTER FUNCTION clinic_app.{_signature} OWNER TO {_owner};
SET LOCAL ROLE {_owner};
REVOKE ALL ON FUNCTION clinic_app.{_signature} FROM PUBLIC,clinic_app,clinic_agent;
"""
    if _public:
        _function_acl += (
            f"GRANT EXECUTE ON FUNCTION clinic_app.{_signature} TO clinic_app;\n"
        )
    if _owner == "clinic_owner":
        _function_acl += (
            f"GRANT EXECUTE ON FUNCTION clinic_app.{_signature} TO clinic_resolver;\n"
        )
    elif _signature in {
        "enterprise_org_admin(uuid)",
        "enterprise_directory_authorized(uuid)",
        "enterprise_scim_audit_scope(uuid)",
        "enterprise_unit_denied(uuid,uuid)",
    }:
        _function_acl += (
            f"GRANT EXECUTE ON FUNCTION clinic_app.{_signature} TO clinic_owner;\n"
        )
    _function_acl += "RESET ROLE;\n"
_scope = "organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid"
_admin = "clinic_app.enterprise_org_admin(clinic_id)"
_manager = (
    "(clinic_app.has_permission('staff.clinic',clinic_id,NULL) OR "
    "clinic_app.has_permission('staff.organization',clinic_id,NULL))"
)
for _table in TABLES:
    _read = _manager
    _write = _admin
    if _table in {"identity_scimtoken", "identity_ssoconnection"}:
        _read = _admin
        if _table == "identity_ssoconnection":
            _read += (
                " OR id=NULLIF(current_setting("
                "'app.current_sso_connection',true),'')::uuid"
                " OR clinic_app.enterprise_directory_authorized(id)"
            )
    elif _table in {"identity_scimresource", "identity_scimmembership"}:
        _read = (
            "clinic_app.enterprise_directory_authorized(connection_id)"
            if _table == "identity_scimresource"
            else "EXISTS(SELECT 1 FROM clinic_app.identity_scimresource g "
            "WHERE g.id=group_id AND "
            "clinic_app.enterprise_directory_authorized(g.connection_id))"
        )
        _write = _read
    elif _table == "identity_breakglassgrant":
        _read = (
            f"{_manager} OR clinician_id=NULLIF("
            "current_setting('app.current_user_id',true),'')::uuid"
        )
        _write = _read
    elif _table == "identity_governancenotice":
        _read = """
__MANAGER__ OR EXISTS(SELECT 1 FROM clinic_app.identity_breakglassgrant g
 WHERE g.id=break_glass_id AND g.clinician_id=NULLIF(
 current_setting('app.current_user_id',true),'')::uuid)
""".replace("__MANAGER__", _manager)
        _write = _read
    _sql += f"""
CREATE POLICY enterprise_read ON clinic_app.{_table} FOR SELECT TO clinic_app
 USING ({_scope} AND ({_read}));
"""
    if "INSERT" in RUNTIME_GRANTS[_table]:
        _sql += f"""
CREATE POLICY enterprise_insert ON clinic_app.{_table} FOR INSERT TO clinic_app
 WITH CHECK ({_scope} AND ({_write}));
"""
    if any(table == _table for table, _col, _priv in COLUMN_GRANTS):
        _sql += f"""
CREATE POLICY enterprise_update ON clinic_app.{_table} FOR UPDATE TO clinic_app
 USING ({_scope} AND ({_write})) WITH CHECK ({_scope} AND ({_write}));
"""
    _sql += f"""
CREATE TRIGGER enterprise_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.{_table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.enterprise_governance_guard();
CREATE TRIGGER enterprise_no_truncate BEFORE TRUNCATE ON clinic_app.{_table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.enterprise_governance_guard();
"""
SQL = _sql + _function_acl + CONTEXT_FENCE_SQL + AUDIT_SQL

_populated = "\n OR ".join(
    "EXISTS (SELECT 1 FROM clinic_app.__TABLE__)".replace("__TABLE__", table)
    for table in TABLES
)
_reverse = """
SET LOCAL ROLE clinic_resolver;
DO $populated$
BEGIN
 IF __POPULATED__ THEN
 RAISE EXCEPTION 'populated enterprise identity rolls back only by restore'
 USING ERRCODE='55000';
 END IF;
END $populated$;
RESET ROLE;
""".replace("__POPULATED__", _populated)
_reverse += PRIOR_AUDIT_SQL + CONTEXT_UNFENCE_SQL + PRIOR_GUARD_SQL
_reverse += """
DROP TRIGGER enterprise_unit_membership ON clinic_app.identity_userclinicrole;
DROP TRIGGER enterprise_unit_policy ON clinic_app.identity_unitgovernancechange;
DROP TRIGGER enterprise_grant_complete ON clinic_app.identity_breakglassgrant;
"""
for _table in TABLES:
    _reverse += f"""
DROP TRIGGER enterprise_no_truncate ON clinic_app.{_table};
DROP TRIGGER enterprise_guard ON clinic_app.{_table};
DROP POLICY enterprise_read ON clinic_app.{_table};
DROP POLICY enterprise_owner ON clinic_app.{_table};
"""
    if "INSERT" in RUNTIME_GRANTS[_table]:
        _reverse += f"DROP POLICY enterprise_insert ON clinic_app.{_table};\n"
    if any(table == _table for table, _col, _priv in COLUMN_GRANTS):
        _reverse += f"DROP POLICY enterprise_update ON clinic_app.{_table};\n"
# Only the task columns this migration added; INSERT and the state columns stay
# with the retention and data-request bridges that granted them first.
_reverse += """
REVOKE UPDATE(owner_user_id,owner_role,last_command_key,last_command_digest)
 ON clinic_app.workflows_task FROM clinic_resolver;
"""
for _signature in SIGNATURES:
    _owner = "clinic_owner" if _signature in OWNER_FUNCTIONS else "clinic_resolver"
    _reverse += f"""SET LOCAL ROLE {_owner};
DROP FUNCTION clinic_app.{_signature};
RESET ROLE;
"""
REVERSE_SQL = _reverse
