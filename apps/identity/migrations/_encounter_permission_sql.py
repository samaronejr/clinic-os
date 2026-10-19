"""Bundle v2 (todo 27): ``encounter.open_unscheduled`` for registered physicians.

The prior resolver preserves the workflow branch and current-membership
predicate installed by 0030. The subtraction CHECK retains the workflow
alternatives installed by 0017. Only the unscheduled permission is added;
the reverse restores those prior contracts, not the released v1 resolver.
"""

from apps.identity.migrations._permission_sql import SQL as V1_SQL
from apps.identity.migrations._workflow_eligibility_sql import SQL as WORKFLOW_SQL

V2_PERMISSIONS = ("encounter.open_unscheduled",)

_FUNCTION_START = WORKFLOW_SQL.index(
    "CREATE OR REPLACE FUNCTION clinic_app.has_permission("
)
_FUNCTION_END = WORKFLOW_SQL.index("END $f$;", _FUNCTION_START) + len("END $f$;")
PRIOR_FUNCTION = WORKFLOW_SQL[_FUNCTION_START:_FUNCTION_END].replace(
    "WHERE r.user_id=actor AND r.clinic_id=clinic AND r.organization_id=tenant",
    "WHERE r.revoked_at IS NULL\n"
    "  AND r.user_id=actor AND r.clinic_id=clinic AND r.organization_id=tenant",
    1,
)
_BUNDLE_START = V1_SQL.index("ADD CONSTRAINT identity_rolegrant_bundle")
_CHECK_START = V1_SQL.index("CHECK (", _BUNDLE_START) + len("CHECK (")
_CHECK_END = V1_SQL.index("ELSE false END);", _CHECK_START) + len("ELSE false END")
V1_BUNDLE_CHECK = V1_SQL[_CHECK_START:_CHECK_END]
PRIOR_BUNDLE_CHECK = (
    V1_BUNDLE_CHECK
    + """
 OR (bundle_version=2 AND (
 (role IN ('owner','org_admin','clinic_admin','clinic_manager',
 'physician','nurse','allied_professional','receptionist','scheduler',
 'finance') AND permission IN ('tasks.view','tasks.assign','tasks.complete'))
 OR (role IN ('owner','org_admin','clinic_admin','clinic_manager')
 AND permission='tasks.reassign')))
"""
)

# The early branch runs after the shared actor, tenant, clinic and enrollment
# checks, so an inactive actor, a foreign clinic or a foreign enrollment is
# refused before it. It is professional (current registration) but not
# patient-scoped: a walk-in may be the patient's first contact.
_ANCHOR = " ) THEN RETURN false; END IF;\n RETURN EXISTS (\n"
_V2_BRANCH = """ ) THEN RETURN false; END IF;
 IF perm=ANY(ARRAY['encounter.open_unscheduled']::text[]) THEN
  RETURN EXISTS (
   SELECT 1 FROM clinic_app.identity_userclinicrole r
   WHERE r.revoked_at IS NULL
    AND r.user_id=actor AND r.clinic_id=clinic AND r.organization_id=tenant
    AND r.role='physician'
    AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_rolegrant g
     WHERE g.organization_id=tenant AND g.clinic_id=clinic AND g.role=r.role
      AND g.bundle_version=2 AND g.permission=perm AND g.effect='remove'
      AND g.valid_from<=checked_at AND (g.valid_to IS NULL OR checked_at<g.valid_to))
    AND EXISTS (
     SELECT 1 FROM clinic_app.identity_professionalregistration p
     JOIN clinic_app.identity_clinic c ON c.id=p.clinic_id
     WHERE p.organization_id=tenant AND p.clinic_id=clinic AND p.user_id=actor
      AND p.role=r.role AND p.jurisdiction=c.crm_uf AND p.status='regular'
      AND p.synthetic AND p.revoked_at IS NULL
      AND p.valid_from<=checked_at AND checked_at<p.valid_to
      AND (p.physician_profile_id IS NULL OR EXISTS (
       SELECT 1 FROM clinic_app.identity_physicianprofile legacy
       WHERE legacy.id=p.physician_profile_id AND legacy.organization_id=tenant
        AND legacy.user_id=actor AND legacy.jurisdiction=p.jurisdiction
        AND legacy.synthetic AND legacy.status='regular'
        AND legacy.last_checked_at<=checked_at AND checked_at<legacy.recheck_at
        AND (legacy.expires_at IS NULL OR checked_at<legacy.expires_at))))
  );
 END IF;
 RETURN EXISTS (
"""
if PRIOR_FUNCTION.count(_ANCHOR) != 1:  # pragma: no cover - import-time contract
    message = "prior has_permission anchor moved"
    raise RuntimeError(message)
V2_FUNCTION = PRIOR_FUNCTION.replace(_ANCHOR, _V2_BRANCH, 1)

_GRANTS = """REVOKE ALL ON FUNCTION clinic_app.has_permission(text,uuid,uuid)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.has_permission(text,uuid,uuid) TO clinic_app;
"""


def _replace(function: str) -> str:
    return (
        "SET LOCAL ROLE clinic_resolver;\n"
        + function.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
        + "\n"
        + _GRANTS
        + "RESET ROLE;\n"
    )


def _bundle(check: str) -> str:
    return (
        "ALTER TABLE clinic_app.identity_rolegrant "
        "DROP CONSTRAINT identity_rolegrant_bundle;\n"
        "ALTER TABLE clinic_app.identity_rolegrant "
        f"ADD CONSTRAINT identity_rolegrant_bundle\n CHECK ({check});\n"
    )


SQL = _replace(V2_FUNCTION) + _bundle(
    f"({PRIOR_BUNDLE_CHECK}) OR (bundle_version=2 AND "
    "role='physician' AND permission=ANY(ARRAY['encounter.open_unscheduled']"
    "::text[]))"
)
REVERSE_SQL = _replace(PRIOR_FUNCTION) + _bundle(PRIOR_BUNDLE_CHECK)
