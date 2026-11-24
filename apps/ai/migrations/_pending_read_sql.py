"""One consent projection for FORCE RLS, privileged export and cached downloads."""

from apps.ai.migrations._care_sql import _edit, _function
from apps.ai.migrations._pending_choice_sql import CHOICE, RELATIONS, SUBJECT
from apps.ai.migrations._pending_refusal_sql import SQL as REFUSAL
from apps.interop.migrations._export_sql import _BRANCHES, _branch
from apps.interop.migrations._export_v2_sql import _V1_READER, _sealed
from apps.interop.migrations._export_v5_sql import EXPORT_V5
from apps.interop.migrations._export_v5_sql import FORWARD as PRIOR_EXPORT
from apps.interop.migrations._interop_sql import SQL as INTEROP

_NAMES = ",".join(f"'{name}'" for name in RELATIONS)
_PRIVATE = """
REVOKE ALL ON FUNCTION clinic_app.ai_pending_choice(text,uuid),
 clinic_app.ai_pending_subject(uuid,uuid,uuid,uuid,uuid,timestamptz,boolean,text)
 FROM PUBLIC,clinic_app,clinic_agent,clinic_owner;
"""

# Only the migration compiler reads policies and emits static SQL. Nothing
# supplied by the runtime caller is interpolated into a query. A public
# boolean-by-id must not be an oracle for records hidden by ordinary RLS.
# Derive the initial audience from the deployed policies, including their
# permissive OR / restrictive AND combination and the separate agent role.
_VISIBLE = """
DO $install$
DECLARE nm text; actor text; allow_expr text; restrict_expr text;
 body text:=$body$
DECLARE eligible boolean;
 actual name:=coalesce(nullif(current_setting('role',true),'none'),session_user);
BEGIN
 IF nullif(current_setting('app.current_tenant',true),'') IS NULL THEN
  RETURN false;
 END IF;
 -- The effective login may inherit the machine role. The original audience
 -- still checks session_user, exact principal, scope and its current grants.
 IF pg_has_role(actual,'clinic_agent','USAGE') THEN actual:='clinic_agent'; END IF;
 CASE
$body$;
BEGIN
 FOREACH nm IN ARRAY ARRAY[__NAMES__] LOOP
  FOREACH actor IN ARRAY ARRAY['clinic_app','clinic_agent'] LOOP
   IF NOT has_table_privilege(actor,format('clinic_app.%I',nm),'SELECT')
    THEN CONTINUE; END IF;
   SELECT string_agg('('||coalesce(pg_get_expr(p.polqual,p.polrelid),'true')||')',
                     ' OR ' ORDER BY p.polname) FILTER (WHERE p.polpermissive),
          string_agg('('||coalesce(pg_get_expr(p.polqual,p.polrelid),'true')||')',
                     ' AND ' ORDER BY p.polname) FILTER (WHERE NOT p.polpermissive)
   INTO allow_expr,restrict_expr FROM pg_policy p
    WHERE p.polrelid=format('clinic_app.%I',nm)::regclass AND p.polcmd IN ('*','r')
      AND (0::oid=ANY(p.polroles) OR EXISTS(SELECT 1 FROM unnest(p.polroles) x
           WHERE pg_has_role(actor,x,'USAGE')));
   IF allow_expr IS NULL THEN CONTINUE; END IF;
   body:=body||format(' WHEN relation_name=%L AND actual=%L THEN
    SELECT EXISTS(SELECT 1 FROM clinic_app.%I WHERE id=requested AND (%s) AND (%s))
    INTO eligible;
',nm,actor,nm,allow_expr,coalesce(restrict_expr,'true'));
  END LOOP;
 END LOOP;
 body:=body||$body$
 ELSE RETURN false;
 END CASE;
 IF NOT eligible THEN RETURN false; END IF;
 RETURN clinic_app.ai_pending_choice(relation_name,requested);
END
$body$;
 EXECUTE format('CREATE FUNCTION clinic_app.ai_pending_visible(relation_name text,
  requested uuid) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
  SET search_path=pg_catalog,clinic_app,pg_temp AS %L',body);
END $install$;
REVOKE ALL ON FUNCTION clinic_app.ai_pending_visible(text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_pending_visible(text,uuid)
 TO clinic_app,clinic_agent;
""".replace("__NAMES__", _NAMES)

_POLICIES = "".join(
    f"CREATE POLICY pending_output_read ON clinic_app.{t} AS RESTRICTIVE "
    "FOR SELECT TO clinic_app,clinic_agent "
    f"USING (clinic_app.ai_pending_visible('{t}',id) "
    "AND clinic_app.enterprise_staff_context());\n"
    for t in RELATIONS
)
_DROP_POLICIES = "".join(
    f"DROP POLICY pending_output_read ON clinic_app.{t};\n" for t in RELATIONS
)
_PART_POLICY = """
CREATE POLICY pending_archive_read ON clinic_app.interop_organizationexportpart
 AS RESTRICTIVE FOR SELECT TO clinic_app USING (
 clinic_app.ai_archive_current(export_id) AND clinic_app.enterprise_staff_context());
"""


def _protected_branch(
    relation: str, key: str, kind: str, omitted: tuple[str, ...], scope: str
) -> str:
    """Keep archive version, original authority, redactions and pagination."""
    if relation == "ai_aiartifact":
        omitted = (*omitted, "source_ref", "source_acceptance")
    elif relation == "interop_organizationexport":
        omitted = (*omitted, "pending_origins")
    result = _sealed(
        _branch(
            relation,
            key,
            kind,
            omitted,
            scope,
            "IF" if relation == EXPORT_V5[0][0] else "ELSIF",
        ),
        relation,
    )
    if relation not in RELATIONS:
        return result
    return _edit(
        result,
        "  AND (after_key IS NULL",
        f"  AND clinic_app.ai_pending_choice('{relation}',t.id)\n"
        "  AND (after_key IS NULL",
    )


_EXPORT = _V1_READER.replace(
    _BRANCHES, "\n".join(_protected_branch(*entry) for entry in EXPORT_V5)
)
_ARCHIVE = """
CREATE FUNCTION clinic_app.ai_archive_current(requested uuid)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE org uuid:=nullif(current_setting('app.current_tenant',true),'')::uuid;
 origins jsonb; scope jsonb;
BEGIN
 IF NOT clinic_app.enterprise_staff_context()
  OR clinic_app.interop_manager(org) IS NOT TRUE THEN RETURN false; END IF;
 SELECT j.pending_origins INTO origins FROM clinic_app.interop_organizationexport j
 WHERE j.organization_id=org AND j.id=requested AND j.state='ready'
   AND j.expires_at>statement_timestamp();
 IF NOT FOUND OR origins IS NULL OR jsonb_typeof(origins)<>'array' THEN
  RETURN false;
 END IF;
 FOR scope IN SELECT value FROM jsonb_array_elements(origins) LOOP
  IF NOT clinic_app.ai_pending_choice(scope->>0,(scope->>1)::uuid) THEN
   RETURN false;
  END IF;
 END LOOP;
 RETURN true;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_archive_current(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_archive_current(uuid) TO clinic_app;
"""
_PRIOR_GUARD = _function(INTEROP, "interop_partner_guard")
_GUARD = _edit(
    _edit(
        _PRIOR_GUARD,
        "IF NEW.state<>'requested' OR NEW.revision<>1",
        "IF NEW.pending_origins IS NOT NULL OR NEW.state<>'requested' "
        "OR NEW.revision<>1",
    ),
    "  IF NEW.revision=OLD.revision+1\n"
    "   AND (to_jsonb(NEW)-ARRAY['state','revision','started_at'",
    """  IF NEW.pending_origins IS DISTINCT FROM OLD.pending_origins
   AND NOT (OLD.state='building' AND NEW.state='ready'
    AND jsonb_typeof(NEW.pending_origins)='array') THEN
   RAISE EXCEPTION 'immutable export dependencies' USING ERRCODE='23514';
  END IF;
  IF NEW.revision=OLD.revision+1
   AND (to_jsonb(NEW)-ARRAY['pending_origins','state','revision','started_at'""",
)
_GUARD = _edit(
    _GUARD,
    "=(to_jsonb(OLD)-ARRAY['state','revision','started_at'",
    "=(to_jsonb(OLD)-ARRAY['pending_origins','state','revision','started_at'",
)
SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + SUBJECT
    + CHOICE
    + _PRIVATE
    + _VISIBLE
    + REFUSAL
    + _ARCHIVE
    + _GUARD
    + "\nRESET ROLE;\n"
    + _POLICIES
    + _PART_POLICY
    + _EXPORT
)
REVERSE_SQL = (
    PRIOR_EXPORT + _DROP_POLICIES + "DROP POLICY IF EXISTS pending_archive_read "
    "ON clinic_app.interop_organizationexportpart;\n"
    + "SET LOCAL ROLE clinic_resolver;\n"
    + _PRIOR_GUARD
    + "\nDROP FUNCTION IF EXISTS clinic_app.ai_archive_current(uuid),"
    "clinic_app.ai_pending_refusal(uuid,uuid,text,uuid,uuid,uuid,text,text),"
    "clinic_app.ai_pending_visible(text,uuid),clinic_app.ai_pending_choice(text,uuid),"
    "clinic_app.ai_pending_subject(uuid,uuid,uuid,uuid,uuid,timestamptz,boolean,text);"
    "\nRESET ROLE;\n"
)
