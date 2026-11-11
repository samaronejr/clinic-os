"""Frozen import rollback activity v2: v1 plus identity 0051's break-glass grants.

The import rollback (``interop_hide_reversible``) hides only imported enrollments
that nothing else references. Its v1 check is compiled from ``_import_sql.py``,
frozen with the irreversible ``interop.0001``, so a patient or enrollment
reference created after it needs a later version. Identity 0051 (todo 65) adds
the first one: a break-glass grant names the enrollment whose chart a clinician
opened in an emergency, outside any care relationship. That is clinical use, so
v2 counts it as enrollment activity and rollback retains the enrollment for
review. Identity 0051 applies v2 right after it creates the grants, so 0051
stays every app's last migration and its populated-rollback refusal covers v2
too. 0051 already grants clinic_resolver SELECT on the grants, so v2 changes no
privilege. The function is replaced in place under its owner, and the reverse
restores the v1 body byte for byte. This module never changes either.
"""

from typing import Final

from apps.interop.migrations._import_sql import FUNCTIONS, _exists, activity_predicate

# Enrollment references created after interop.0001, in v1's (table, column) shape.
ENROLLMENT_ACTIVITY: Final = (("identity_breakglassgrant", "enrollment_id"),)
_START: Final = FUNCTIONS.index("CREATE FUNCTION clinic_app.interop_hide_reversible(")
_END: Final = FUNCTIONS.index("END $f$;\n", _START) + len("END $f$;\n")
_V1: Final = FUNCTIONS[_START:_END].replace(
    "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
)
_V1_ACTIVITY: Final = activity_predicate("c")
_V2_ACTIVITY: Final = _V1_ACTIVITY + "".join(
    "\n  OR " + _exists(table, f"x.{column}=c.enrollment_id")
    for table, column in ENROLLMENT_ACTIVITY
)
FORWARD: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _V1.replace(_V1_ACTIVITY, _V2_ACTIVITY)
    + "RESET ROLE;\n"
)
REVERSE: Final = "SET LOCAL ROLE clinic_resolver;\n" + _V1 + "RESET ROLE;\n"
