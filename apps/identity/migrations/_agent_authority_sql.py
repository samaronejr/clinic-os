"""V2 machine execution, and one shared permission evaluator for bound identities."""

from apps.identity.migrations._principal_sql import SQL as PRINCIPAL_V1
from apps.identity.migrations._reception_permission_sql import SQL as PERMISSION_SQL

_FUNCTION_START = PERMISSION_SQL.index(
    "CREATE OR REPLACE FUNCTION clinic_app.has_permission("
)
_FUNCTION_END = PERMISSION_SQL.index("END $f$;", _FUNCTION_START) + len("END $f$;")
V2_FUNCTION = PERMISSION_SQL[_FUNCTION_START:_FUNCTION_END]
_CONTEXT_START = V2_FUNCTION.index(" BEGIN\n  actor :=")
_CONTEXT_END = V2_FUNCTION.index(" IF actor IS NULL")
_EVALUATOR = (
    (V2_FUNCTION[:_CONTEXT_START] + V2_FUNCTION[_CONTEXT_END:])
    .replace(
        "CREATE OR REPLACE FUNCTION clinic_app.has_permission(\n"
        " perm text, clinic uuid, enrollment uuid)",
        "CREATE FUNCTION clinic_app.identity_actor_permission(\n"
        " perm text, clinic uuid, enrollment uuid, actor uuid, tenant uuid)",
    )
    .replace(
        "DECLARE actor uuid; tenant uuid; checked_at",
        "DECLARE checked_at",
    )
)
_PRINCIPAL_START = PRINCIPAL_V1.index("CREATE FUNCTION clinic_app.principal_scope(")
_PRINCIPAL_END = PRINCIPAL_V1.index("$f$;", _PRINCIPAL_START) + len("$f$;")
_PRINCIPAL = (
    PRINCIPAL_V1[_PRINCIPAL_START:_PRINCIPAL_END]
    .replace(
        "CREATE FUNCTION",
        "CREATE OR REPLACE FUNCTION",
        1,
    )
    .replace("p.grant_set_version=1", "p.grant_set_version IN (1,2)")
    .replace(
        "AND g.active AND g.subject_scope='clinic' AND g.permission='appointment.read'",
        "AND g.active AND g.subject_scope='clinic'\n"
        "   AND g.grant_set_version=p.grant_set_version\n"
        "   AND ((p.grant_set_version=1 AND g.permission='appointment.read')\n"
        "     OR (p.grant_set_version=2 AND g.permission='agent.execute'))",
    )
)

_SQL_PARTS = (
    """CREATE FUNCTION clinic_app.identity_agent_grant_version()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF EXISTS(SELECT 1 FROM clinic_app.identity_serviceprincipal p
  WHERE p.id=NEW.principal_id AND p.organization_id=NEW.organization_id
   AND p.grant_set_version<>NEW.grant_set_version) THEN
  RAISE EXCEPTION 'principal grant version mismatch' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.identity_agent_grant_version() FROM PUBLIC;
CREATE TRIGGER principal_grant_version BEFORE INSERT
 ON clinic_app.identity_serviceprincipalgrant FOR EACH ROW
 EXECUTE FUNCTION clinic_app.identity_agent_grant_version();
SET LOCAL ROLE clinic_resolver;
""",
    _EVALUATOR,
    """
REVOKE ALL ON FUNCTION clinic_app.identity_actor_permission(text,uuid,uuid,uuid,uuid)
 FROM PUBLIC;
CREATE OR REPLACE FUNCTION clinic_app.has_permission(
 perm text, clinic uuid, enrollment uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid; tenant uuid;
BEGIN
 BEGIN
  actor := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  tenant := NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 EXCEPTION WHEN invalid_text_representation THEN RETURN false;
 END;
 RETURN clinic_app.identity_actor_permission(perm,clinic,enrollment,actor,tenant);
END $f$;
""",
    _PRINCIPAL,
    "\nRESET ROLE;\n",
)

SQL = "".join(_SQL_PARTS)
