"""Frozen import rollback activity v3: v2 plus identity 0052's delegate reads.

A delegate-access row (todo 73, DG-R1) records that a delegate read an
enrollment's released records or results. That is use of the enrollment, so
v3 counts it as activity and import rollback retains the enrollment for review.
Identity 0052 applies v3 right after it creates the table (clinic_resolver
already reads it) and its reverse restores v2's body byte for byte.
"""

from typing import Final

from apps.interop.migrations._import_activity_v2_sql import (
    _V1,
    _V1_ACTIVITY,
    _V2_ACTIVITY,
)
from apps.interop.migrations._import_sql import _exists

# Enrollment references created after identity 0051, in v1's (table, column) shape.
ENROLLMENT_ACTIVITY: Final = (("identity_delegateaccessevent", "enrollment_id"),)
_V3_ACTIVITY: Final = _V2_ACTIVITY + "".join(
    "\n  OR " + _exists(table, f"x.{column}=c.enrollment_id")
    for table, column in ENROLLMENT_ACTIVITY
)
FORWARD: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _V1.replace(_V1_ACTIVITY, _V3_ACTIVITY)
    + "RESET ROLE;\n"
)
REVERSE: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _V1.replace(_V1_ACTIVITY, _V2_ACTIVITY)
    + "RESET ROLE;\n"
)
