"""Add one v2 permission to the installed evaluator without replaying old SQL."""

from apps.identity.migrations._encounter_permission_sql import _V2_BRANCH

_ANCHOR = " IF perm='reception.queue' THEN"
# The clock census's finite RunSQL decoder has neither str.rindex nor psycopg
# composition: keep everything before the branch template's last RETURN EXISTS.
_BRANCH = " RETURN EXISTS (".join(
    _V2_BRANCH[_V2_BRANCH.index(" IF perm=") :].split(" RETURN EXISTS (")[:-1]
).replace(
    "perm=ANY(ARRAY['encounter.open_unscheduled']::text[])",
    "perm='tiss.clinical_approve'",
)

# Read the installed definitions so independently additive v2 permissions and
# the shared human/agent evaluator survive this migration in either lane order.
# The fixed anchor is checked before replacing; a changed contract fails closed.
# String literals quoted exactly as psycopg's sql.Literal renders text that
# contains no backslash: single quotes doubled inside single quotes.
_ANCHOR_SQL = "'" + _ANCHOR.replace("'", "''") + "'"
_BRANCH_SQL = "'" + _BRANCH.replace("'", "''") + "'"
SQL = f"""
SET LOCAL ROLE clinic_resolver;
DO $migration$
DECLARE definition text; anchor text := {_ANCHOR_SQL}; branch text := {_BRANCH_SQL};
BEGIN
 SELECT pg_get_functiondef(
  'clinic_app.identity_actor_permission(text,uuid,uuid,uuid,uuid)'::regprocedure)
 INTO definition;
 IF strpos(definition,branch)=0 THEN
  IF (length(definition)-length(replace(definition,anchor,'')))<>length(anchor) THEN
   RAISE EXCEPTION 'current permission evaluator anchor moved';
  END IF;
  EXECUTE replace(definition,anchor,branch||anchor);
 END IF;
END $migration$;
RESET ROLE;
DO $migration$
DECLARE definition text;
BEGIN
 SELECT pg_get_expr(conbin,conrelid) INTO STRICT definition
 FROM pg_constraint
 WHERE conrelid='clinic_app.identity_rolegrant'::regclass
  AND conname='identity_rolegrant_bundle';
 IF strpos(definition,'tiss.clinical_approve')=0 THEN
  ALTER TABLE clinic_app.identity_rolegrant
   DROP CONSTRAINT identity_rolegrant_bundle;
  EXECUTE 'ALTER TABLE clinic_app.identity_rolegrant ADD CONSTRAINT '
   ||'identity_rolegrant_bundle CHECK (('||definition||') OR '
   ||'(bundle_version=2 AND role=''physician'' '
   ||'AND permission=''tiss.clinical_approve''))';
 END IF;
END $migration$;
"""  # noqa: S608 - fixed, pre-quoted module literals; no runtime input.
