"""Bundle v2 (todo 27): ``encounter.open_unscheduled`` for registered physicians.

The v1 resolver body and the v1 subtraction CHECK are sliced verbatim from the
frozen v1 source, so v2 cannot drift from v1 for any v1 permission. v2 adds
one early branch for the new permission and one constraint alternative that
lets a clinic narrow it with a version-2 subtraction. The reverse restores the
exact v1 bytes; it fails while version-2 subtractions exist (restore instead).
"""

from apps.identity.migrations._permission_sql import SQL as V1_SQL

V2_PERMISSIONS = ("encounter.open_unscheduled",)

_FUNCTION_START = V1_SQL.index("CREATE FUNCTION clinic_app.has_permission(")
_FUNCTION_END = V1_SQL.index("END $f$;", _FUNCTION_START) + len("END $f$;")
V1_FUNCTION = V1_SQL[_FUNCTION_START:_FUNCTION_END]
_BUNDLE_START = V1_SQL.index("ADD CONSTRAINT identity_rolegrant_bundle")
_CHECK_START = V1_SQL.index("CHECK (", _BUNDLE_START) + len("CHECK (")
_CHECK_END = V1_SQL.index("ELSE false END);", _CHECK_START) + len("ELSE false END")
V1_BUNDLE_CHECK = V1_SQL[_CHECK_START:_CHECK_END]

# The early branch runs after the shared actor, tenant, clinic and enrollment
# checks, so an inactive actor, a foreign clinic or a foreign enrollment is
# refused before it. It is professional (current registration) but not
# patient-scoped: a walk-in may be the patient's first contact.
_ANCHOR = " ) THEN RETURN false; END IF;\n RETURN EXISTS (\n"
_V2_BRANCH = """ ) THEN RETURN false; END IF;
 IF perm=ANY(ARRAY['encounter.open_unscheduled']::text[]) THEN
  RETURN EXISTS (
   SELECT 1 FROM clinic_app.identity_userclinicrole r
   WHERE r.user_id=actor AND r.clinic_id=clinic AND r.organization_id=tenant
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
if V1_FUNCTION.count(_ANCHOR) != 1:  # pragma: no cover - import-time contract
    message = "v1 has_permission anchor moved"
    raise RuntimeError(message)
V2_FUNCTION = V1_FUNCTION.replace(_ANCHOR, _V2_BRANCH, 1)

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
    f"(bundle_version=1 AND ({V1_BUNDLE_CHECK})) OR (bundle_version=2 AND "
    "role='physician' AND permission=ANY(ARRAY['encounter.open_unscheduled']"
    "::text[]))"
)
REVERSE_SQL = _replace(V1_FUNCTION) + _bundle(V1_BUNDLE_CHECK)
