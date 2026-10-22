"""Add only reception queue/room authority; preserve all appointment terms."""

from apps.identity.migrations._encounter_permission_sql import (
    PRIOR_BUNDLE_CHECK,
    V2_FUNCTION,
    _bundle,
    _replace,
)

_ANCHOR = " IF perm=ANY(ARRAY['encounter.open_unscheduled']::text[]) THEN"
_BRANCH = """ IF perm='reception.queue' THEN
  RETURN EXISTS (
   SELECT 1 FROM clinic_app.identity_userclinicrole r
   WHERE r.organization_id=tenant AND r.clinic_id=clinic AND r.user_id=actor
    AND r.revoked_at IS NULL AND r.role IN ('receptionist','nurse')
    AND NOT EXISTS (
     SELECT 1 FROM clinic_app.identity_rolegrant g
     WHERE g.organization_id=tenant AND g.clinic_id=clinic AND g.role=r.role
      AND g.bundle_version=2 AND g.permission=perm AND g.effect='remove'
      AND g.valid_from<=checked_at AND (g.valid_to IS NULL OR checked_at<g.valid_to))
  );
 END IF;
"""
if V2_FUNCTION.count(_ANCHOR) != 1:
    message = "prior encounter permission branch moved"
    raise RuntimeError(message)

_PRIOR_CHECK = (
    f"({PRIOR_BUNDLE_CHECK}) OR (bundle_version=2 AND "
    "role='physician' AND permission='encounter.open_unscheduled')"
)
SQL = _replace(V2_FUNCTION.replace(_ANCHOR, _BRANCH + _ANCHOR, 1)) + _bundle(
    f"({_PRIOR_CHECK}) OR (bundle_version=2 AND "
    "role IN ('receptionist','nurse') AND permission='reception.queue')"
)
REVERSE_SQL = _replace(V2_FUNCTION) + _bundle(_PRIOR_CHECK)
