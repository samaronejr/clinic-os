"""Plan item 27 round 3: an addendum save receipt needs an open encounter.

``ehr_addendum_guard`` (``_addendum_sql``) bound addendum rows to an open
encounter but checked only the addendum for receipts. An unchanged save
writes no addendum row, so after closure it still created a new receipt: a
new accepted command on a closed encounter. The receipt branch now loads the
addendum's encounter and refuses unless it is ``open``.

Both directions are derived from the installed body, so the reverse restores
the exact prior definition (SC-17): the forward replaces only the receipt
condition, and ``PRIOR`` is the 0011 text itself.
"""

from apps.ehr.migrations._addendum_sql import _GUARD

_RECEIPT_PRIOR = (
    "   -- A receipt acknowledges the revision the same transaction committed.\n"
    "   IF addendum_row.id IS NULL\n"
    "      OR addendum_row.organization_id <> NEW.organization_id\n"
    "      OR addendum_row.state <> 'draft'\n"
)
_RECEIPT_OPEN = """   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = addendum_row.encounter_id;
   -- A receipt acknowledges the revision the same transaction committed,
   -- and only while the encounter is open: an unchanged save writes no
   -- addendum row, so this is the only binding its new command meets.
   IF addendum_row.id IS NULL
      OR addendum_row.organization_id <> NEW.organization_id
      OR addendum_row.state <> 'draft'
      OR encounter_row.id IS NULL
      OR encounter_row.state <> 'open'
"""


def _replaceable(body: str) -> str:
    """Turn the 0011 ``CREATE FUNCTION`` into an in-place replacement."""
    if body.count("CREATE FUNCTION") != 1 or body.count(_RECEIPT_PRIOR) != 1:
        msg = "ehr_addendum_guard body differs from the 0011 definition"
        raise RuntimeError(msg)
    return body.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")


PRIOR = _replaceable(_GUARD)
CURRENT = PRIOR.replace(_RECEIPT_PRIOR, _RECEIPT_OPEN)

# The function is owned by clinic_resolver (SECURITY DEFINER); only its owner
# may replace it. CREATE OR REPLACE keeps the owner, ACL and triggers.
SQL = "SET LOCAL ROLE clinic_resolver;\n" + CURRENT + "RESET ROLE;\n"
REVERSE_SQL = "SET LOCAL ROLE clinic_resolver;\n" + PRIOR + "RESET ROLE;\n"
