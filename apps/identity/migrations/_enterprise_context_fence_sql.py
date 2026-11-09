"""Narrow existing policies, retaining names, commands, roles and normal behavior.

Independent SCIM and pre-auth SSO need a tenant for envelope encryption, but
must not inherit a generic staff tenant policy. Support diagnostics also
never become a staff context, even if a future caller accidentally mixes GUCs.

Only policies that apply to clinic_app (PUBLIC or a role clinic_app has the
privileges of) are narrowed: under any other role the fence is constantly true,
so owner-only setup policies and the machine principal's reviewed gates stay
byte for byte. Each fenced expression keeps its meaning: an ALL or UPDATE policy
without WITH CHECK checks new rows with its USING expression, and an absent
expression grants nothing when permissive and restricts nothing when
restrictive. The statements are static, built from the reviewed rows in
_enterprise_fence_policies, so the clock census decodes every one. The forward
refuses to finish while any applicable policy is unfenced, and the unused
rollback cannot drop the fence function while a policy still calls it.
Migration 0051 depends on every app's migrations, so the fence sees every
policy; a policy created after it must carry the fence itself
(tests/identity/test_enterprise_migration).
"""

from apps.identity.enterprise_contract import TABLES

from ._enterprise_fence_policies import POLICIES

_FENCE = " AND clinic_app.enterprise_staff_context())"
_excluded = ",".join("'" + table + "'" for table in TABLES)
_unfenced_query = """
 SELECT 1 FROM pg_catalog.pg_policy p
 JOIN pg_catalog.pg_class c ON c.oid=p.polrelid
 JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='clinic_app' AND c.relrowsecurity
 AND c.relname<>ALL(ARRAY[__excluded__]::text[])
 AND (0::oid=ANY(p.polroles) OR EXISTS (SELECT 1 FROM unnest(p.polroles) r(oid)
 WHERE pg_catalog.pg_has_role('clinic_app',r.oid,'USAGE')))
 AND ((p.polcmd IN ('*','r','w','d') AND coalesce(pg_catalog.pg_get_expr(
 p.polqual,p.polrelid),'') NOT LIKE '%enterprise_staff_context()%')
 OR (p.polcmd IN ('*','a','w') AND coalesce(pg_catalog.pg_get_expr(
 p.polwithcheck,p.polrelid),'') NOT LIKE '%enterprise_staff_context()%'))
""".replace("__excluded__", _excluded)
_FUNCTION = """
CREATE FUNCTION clinic_app.enterprise_staff_context()
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT current_user<>'clinic_app'
 OR (
 NULLIF(current_setting('app.current_scim_token',true),'') IS NULL
 AND NULLIF(current_setting('app.current_sso_connection',true),'') IS NULL
 AND NULLIF(current_setting('app.current_support_grant',true),'') IS NULL)
$f$;
REVOKE ALL ON FUNCTION clinic_app.enterprise_staff_context() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.enterprise_staff_context()
 TO clinic_app,clinic_agent,clinic_resolver;
"""
_fence = "".join(
    f'ALTER POLICY "{policy}" ON clinic_app."{table}"'
    + (f" USING (({using}){_FENCE}" if command in "*rwd" else "")
    + (
        f" WITH CHECK (({using if check is None else check}){_FENCE}"
        if command in "*aw"
        else ""
    )
    + ";\n"
    for table, policy, command, using, check in POLICIES
)
_unfence = "".join(
    f'ALTER POLICY "{policy}" ON clinic_app."{table}"'
    + (f" USING ({using})" if command in "*rwd" else "")
    + (f" WITH CHECK ({using if check is None else check})" if command in "*aw" else "")
    + ";\n"
    for table, policy, command, using, check in POLICIES
)
_CHECK = """DO $policy_fence$
BEGIN
 IF EXISTS (__UNFENCED__) THEN
 RAISE EXCEPTION 'a clinic_app policy has no enterprise fence'
 USING ERRCODE='55000';
 END IF;
END $policy_fence$;
""".replace("__UNFENCED__", _unfenced_query)
_DROP = "DROP FUNCTION clinic_app.enterprise_staff_context();\n"
SQL = f"{_FUNCTION}{_fence}{_CHECK}"
REVERSE_SQL = f"{_unfence}{_DROP}"
