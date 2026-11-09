"""Narrow existing policies, retaining names, commands, roles and normal behavior.

Independent SCIM and pre-auth SSO need a tenant for envelope encryption, but
must not inherit a generic staff tenant policy. Support diagnostics also
never become a staff context, even if a future caller accidentally mixes GUCs.

Each expression a policy is evaluated with keeps its meaning under the fence: an
ALL or UPDATE policy without WITH CHECK checks new rows with its USING
expression, and an absent expression grants nothing when permissive and
restricts nothing when restrictive. Only policies that apply to clinic_app
(PUBLIC or a role clinic_app has the privileges of) are narrowed: under any
other role the fence is constantly true, so owner-only setup policies and the
machine principal's reviewed gates stay byte for byte. A table owned by another
role is altered as its owner. Migration 0051 depends on every app's migrations,
so the fence sees every policy; a policy created after it must carry the fence
itself (tests/identity/test_enterprise_migration). The unused rollback strips
exactly the appended fence again.
"""

from apps.identity.enterprise_contract import TABLES

_excluded = ",".join("'" + table + "'" for table in TABLES)
_policies = """
 SELECT n.nspname,c.relname,p.polname,p.polcmd,p.polpermissive,
 pg_get_userbyid(c.relowner) AS owner,
 pg_get_expr(p.polqual,p.polrelid) AS using_expression,
 pg_get_expr(p.polwithcheck,p.polrelid) AS check_expression
 FROM pg_catalog.pg_policy p
 JOIN pg_catalog.pg_class c ON c.oid=p.polrelid
 JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE n.nspname='clinic_app' AND c.relrowsecurity
 AND c.relname<>ALL(ARRAY[__excluded__]::text[])
 AND (0::oid=ANY(p.polroles) OR EXISTS (SELECT 1 FROM unnest(p.polroles) r(oid)
 WHERE pg_catalog.pg_has_role('clinic_app',r.oid,'USAGE')))
""".replace("__excluded__", _excluded)
SQL = """
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
DO $policy_fence$
DECLARE policy record; clauses text; fallback text; migrator text:=current_user;
BEGIN
 FOR policy IN __POLICIES__
 LOOP
 fallback:=CASE WHEN policy.polpermissive THEN 'false' ELSE 'true' END;
 clauses:='';
 IF policy.polcmd IN ('*','r','w','d') THEN
 clauses:=clauses||format(
 ' USING ((%s) AND clinic_app.enterprise_staff_context())',
 coalesce(policy.using_expression,fallback));
 END IF;
 IF policy.polcmd IN ('*','a','w') THEN
 clauses:=clauses||format(
 ' WITH CHECK ((%s) AND clinic_app.enterprise_staff_context())',
 coalesce(policy.check_expression,policy.using_expression,fallback));
 END IF;
 EXECUTE format('SET LOCAL ROLE %I',policy.owner);
 EXECUTE format('ALTER POLICY %I ON %I.%I%s',policy.polname,
 policy.nspname,policy.relname,clauses);
 EXECUTE format('SET LOCAL ROLE %I',migrator);
 END LOOP;
END $policy_fence$;
""".replace("__POLICIES__", _policies)
REVERSE_SQL = """
DO $policy_unfence$
DECLARE policy record; restored_using text; restored_check text;
 migrator text:=current_user; suffix text;
 suffixes text[]:=ARRAY[' AND enterprise_staff_context())',
 ' AND clinic_app.enterprise_staff_context())'];
BEGIN
 FOR policy IN __POLICIES__
 LOOP
 restored_using:=NULL;
 restored_check:=NULL;
 FOREACH suffix IN ARRAY suffixes LOOP
 IF left(policy.using_expression,1)='('
 AND right(policy.using_expression,length(suffix))=suffix THEN
 restored_using:=substr(policy.using_expression,2,
 length(policy.using_expression)-1-length(suffix));
 END IF;
 IF left(policy.check_expression,1)='('
 AND right(policy.check_expression,length(suffix))=suffix THEN
 restored_check:=substr(policy.check_expression,2,
 length(policy.check_expression)-1-length(suffix));
 END IF;
 END LOOP;
 IF (policy.using_expression IS NULL)<>(restored_using IS NULL)
 OR (policy.check_expression IS NULL)<>(restored_check IS NULL) THEN
 RAISE EXCEPTION 'policy %.% has no enterprise fence',policy.relname,policy.polname
 USING ERRCODE='55000';
 END IF;
 EXECUTE format('SET LOCAL ROLE %I',policy.owner);
 EXECUTE format('ALTER POLICY %I ON %I.%I%s',policy.polname,
 policy.nspname,policy.relname,
 coalesce(' USING ('||restored_using||')','')
 ||coalesce(' WITH CHECK ('||restored_check||')',''));
 EXECUTE format('SET LOCAL ROLE %I',migrator);
 END LOOP;
END $policy_unfence$;
DROP FUNCTION clinic_app.enterprise_staff_context();
""".replace("__POLICIES__", _policies)
