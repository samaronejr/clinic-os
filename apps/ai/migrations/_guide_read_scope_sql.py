"""One guide read predicate for invoker RLS and the protected consent reader."""

from apps.ai.migrations._care_sql import _edit, _function
from apps.ai.migrations._guide_consent_sql import SQL as PRIOR_GUIDE
from apps.identity.migrations._enterprise_context_fence_sql import _FUNCTION
from apps.identity.migrations._enterprise_fence_policies import POLICIES

_PRIOR_FENCE = _function(_FUNCTION, "enterprise_staff_context")
# The GUCs still come from the caller. A NOLOGIN BYPASSRLS resolver does not
# traverse staff RLS, but explicitly calling the predicate inside its definer
# must not exempt that caller. All other effective roles retain their behavior.
_FENCE = _edit(
    _PRIOR_FENCE,
    "current_user<>'clinic_app'",
    "current_user NOT IN ('clinic_app','clinic_resolver')",
)
_PRIOR_READER = _function(PRIOR_GUIDE, "ai_guide_consent")
_GUIDE_READER = _edit(
    _edit(
        _PRIOR_READER,
        "g.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid",
        "clinic_app.insurance_guide_read_scope(g.organization_id,g.clinic_id)",
    ),
    "   AND clinic_app.has_permission('tiss.read',g.clinic_id,NULL)\n",
    "",
)
_SCOPE = """
CREATE FUNCTION clinic_app.insurance_guide_read_scope(org uuid,clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY INVOKER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce(org=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND clinic_app.has_permission('tiss.read',clinic,NULL)
  AND clinic_app.enterprise_staff_context(),false)
$f$;
REVOKE ALL ON FUNCTION clinic_app.insurance_guide_read_scope(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.insurance_guide_read_scope(uuid,uuid)
 TO clinic_app;
"""
_PRIOR_USING = next(
    using
    for table, name, _, using, _ in POLICIES
    if (table, name) == ("insurance_guide", "insurance_scope")
)
_POLICY = "ALTER POLICY insurance_scope ON clinic_app.insurance_guide USING "
# Retain the enterprise migration's outer fence and original WITH CHECK. The
# shared predicate includes the fence independently for non-policy callers.
SQL = (
    _FENCE
    + "\nSET LOCAL ROLE clinic_resolver;\n"
    + _SCOPE
    + _GUIDE_READER
    + "\nRESET ROLE;\n"
    + _POLICY
    + "(clinic_app.insurance_guide_read_scope(organization_id,clinic_id)"
    + " AND clinic_app.enterprise_staff_context());"
)
REVERSE_SQL = (
    _POLICY
    + f"(({_PRIOR_USING}) AND clinic_app.enterprise_staff_context());\n"
    + "SET LOCAL ROLE clinic_resolver;\n"
    + _PRIOR_READER
    + "\nDROP FUNCTION clinic_app.insurance_guide_read_scope(uuid,uuid);\n"
    + "RESET ROLE;\n"
    + _PRIOR_FENCE
)
