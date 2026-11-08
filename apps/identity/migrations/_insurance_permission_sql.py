"""Add one v2 permission to the shared evaluator, composed statically from 0044."""

from apps.identity.migrations import _inventory_permission_sql as prior
from apps.identity.migrations._encounter_permission_sql import _V2_BRANCH, _bundle
from apps.identity.migrations._privacy_permission_sql import _evaluator

_ANCHOR = " IF perm='reception.queue' THEN"
# The clock census's finite RunSQL decoder has neither str.rindex nor psycopg
# composition: keep everything before the branch template's last RETURN EXISTS.
_BRANCH = " RETURN EXISTS (".join(
    _V2_BRANCH[_V2_BRANCH.index(" IF perm=") :].split(" RETURN EXISTS (")[:-1]
).replace(
    "perm=ANY(ARRAY['encounter.open_unscheduled']::text[])",
    "perm='tiss.clinical_approve'",
)
# Compose from 0044's SQL exactly as 0044 composes from 0042's: 0044 is the
# last writer of the shared evaluator and the bundle CHECK before this
# migration, and no later migration rewrites either, so the result is static.
_START = prior.SQL.index(
    "CREATE OR REPLACE FUNCTION clinic_app.identity_actor_permission("
)
_END = prior.SQL.index("END $f$;", _START) + len("END $f$;")
_FUNCTION = prior.SQL[_START:_END]
if _FUNCTION.count(_ANCHOR) != 1:  # pragma: no cover - import-time contract
    message = "prior inventory permission anchor moved"
    raise RuntimeError(message)
_CHECK_START = prior.SQL.index("ADD CONSTRAINT identity_rolegrant_bundle\n CHECK (")
# The decoder has no str.rsplit: rejoin every part before the final ");".
_CHECK = ");".join(prior.SQL[_CHECK_START:].split("CHECK (", 1)[1].split(");")[:-1])
SQL = _evaluator(_FUNCTION.replace(_ANCHOR, _BRANCH + _ANCHOR, 1)) + _bundle(
    f"({_CHECK}) OR (bundle_version=2 AND role='physician' "
    "AND permission='tiss.clinical_approve')"
)
