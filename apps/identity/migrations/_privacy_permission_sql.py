"""Add only data-subject request authority; keep every released permission term."""

from apps.identity.migrations._agent_authority_sql import _EVALUATOR
from apps.identity.migrations._encounter_permission_sql import _bundle
from apps.identity.migrations._reception_permission_sql import (
    _PRIOR_CHECK as _ENCOUNTER_CHECK,
)

# Management answers LGPD requests (todo 50): the clinic manager and clinic
# admin rows of the RP matrix plus the organization rows. Clinics narrow it
# with an explicit v2 remove grant, exactly like the other v2 extensions.
_ROLE_LIST = "'owner','org_admin','clinic_admin','clinic_manager'"
_ANCHOR = " IF perm='reception.queue' THEN"
_BRANCH = """ IF perm='privacy.requests' THEN
  RETURN EXISTS (
   SELECT 1 FROM clinic_app.identity_userclinicrole r
   WHERE r.organization_id=tenant AND r.clinic_id=clinic AND r.user_id=actor
    AND r.revoked_at IS NULL
    AND r.role IN ('owner','org_admin','clinic_admin','clinic_manager')
    AND NOT EXISTS (
     SELECT 1 FROM clinic_app.identity_rolegrant g
     WHERE g.organization_id=tenant AND g.clinic_id=clinic AND g.role=r.role
      AND g.bundle_version=2 AND g.permission=perm AND g.effect='remove'
      AND g.valid_from<=checked_at AND (g.valid_to IS NULL OR checked_at<g.valid_to))
  );
 END IF;
"""
if _EVALUATOR.count(_ANCHOR) != 1:  # pragma: no cover - import-time contract
    message = "prior reception permission branch moved"
    raise RuntimeError(message)

_RECEPTION_CHECK = (
    f"({_ENCOUNTER_CHECK}) OR (bundle_version=2 AND "
    "role IN ('receptionist','nurse') AND permission='reception.queue')"
)


def _evaluator(function: str) -> str:
    return (
        "SET LOCAL ROLE clinic_resolver;\n"
        + function.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
        + "\nREVOKE ALL ON FUNCTION clinic_app.identity_actor_permission("
        "text,uuid,uuid,uuid,uuid) FROM PUBLIC;\nRESET ROLE;\n"
    )


SQL = _evaluator(_EVALUATOR.replace(_ANCHOR, _BRANCH + _ANCHOR, 1)) + _bundle(
    f"({_RECEPTION_CHECK}) OR (bundle_version=2 AND "
    "role IN (" + _ROLE_LIST + ") AND permission='privacy.requests')"
)
REVERSE_SQL = _evaluator(_EVALUATOR) + _bundle(_RECEPTION_CHECK)
