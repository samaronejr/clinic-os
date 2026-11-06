"""Extend the shared v2 evaluator while retaining every Wave D permission."""

from apps.identity.migrations import _privacy_permission_sql as prior
from apps.identity.migrations._encounter_permission_sql import _bundle
from apps.identity.migrations._privacy_permission_sql import _evaluator

START = prior.SQL.index(
    "CREATE OR REPLACE FUNCTION clinic_app.identity_actor_permission("
)
END = prior.SQL.index("END $f$;", START) + len("END $f$;")
FUNCTION = prior.SQL[START:END]
ANCHOR = " IF perm='privacy.requests' THEN"
BRANCH = """
 IF perm=ANY(ARRAY['inventory.read','inventory.use','inventory.manage',
 'inventory.transfer_org']::text[]) THEN
  IF NULLIF(current_setting('app.current_principal',true),'') IS NOT NULL
  THEN RETURN false; END IF;
  RETURN EXISTS(SELECT 1 FROM clinic_app.identity_userclinicrole r
   WHERE r.organization_id=tenant AND r.clinic_id=clinic AND r.user_id=actor
   AND r.revoked_at IS NULL AND (
    (r.role IN ('clinic_manager','clinic_admin') AND
      perm IN ('inventory.read','inventory.use','inventory.manage'))
    OR (r.role='nurse' AND perm IN ('inventory.read','inventory.use'))
    OR (r.role IN ('owner','org_admin') AND perm='inventory.transfer_org'))
   AND NOT EXISTS(SELECT 1 FROM clinic_app.identity_rolegrant g
    WHERE g.organization_id=tenant AND g.clinic_id=clinic AND g.role=r.role
    AND g.bundle_version=2 AND g.permission=perm AND g.effect='remove'
    AND g.valid_from<=checked_at AND (g.valid_to IS NULL OR checked_at<g.valid_to)));
 END IF;
"""
if FUNCTION.count(ANCHOR) != 1:  # pragma: no cover - import-time contract
    message = "prior privacy permission anchor moved"
    raise RuntimeError(message)
CHECK_START = prior.SQL.index("ADD CONSTRAINT identity_rolegrant_bundle\n CHECK (")
CHECK = prior.SQL[CHECK_START:].split("CHECK (", 1)[1].rsplit(");", 1)[0]
SQL = _evaluator(FUNCTION.replace(ANCHOR, BRANCH + ANCHOR, 1)) + _bundle(
    f"({CHECK}) OR (bundle_version=2 AND ("
    "(role IN ('clinic_manager','clinic_admin') AND permission IN "
    "('inventory.read','inventory.use','inventory.manage')) OR "
    "(role='nurse' AND permission IN ('inventory.read','inventory.use')) OR "
    "(role IN ('owner','org_admin') AND permission='inventory.transfer_org')))"
)
REVERSE_SQL = _evaluator(FUNCTION) + _bundle(CHECK)
