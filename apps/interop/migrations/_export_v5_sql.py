"""Frozen export-v5 relation set: export v4 plus todo 73's delegate-access log.

Export v4 (``_export_v4_sql.py``) is frozen, and a new relation needs a new export
version. Todo 73 (threat row DG-R1) added ``identity_delegateaccessevent`` after
v4 was frozen. Each row records one delegate read the represented patient sees
in their access log: the delegate grant, the represented enrollment, the closed
record type and the time. Like the break-glass notices v3 exports, it is the
organization's own access history, so v5 exports it at organization scope with
no omitted column. Identity 0052 applies this reader right after it creates the
table, so its populated-rollback refusal covers v5 too; 0052 grants
clinic_resolver SELECT on the table, so v5 changes no privilege. The reverse is
the v4 reader. This module never changes either.
"""

from typing import Final

from apps.interop.migrations._export_sql import _BRANCHES, _branch
from apps.interop.migrations._export_v2_sql import _V1_READER, _sealed
from apps.interop.migrations._export_v4_sql import EXCLUDED_V4, EXPORT_V4
from apps.interop.migrations._export_v4_sql import FORWARD as V4_READER

# The delegate-access log (todo 73), in export v1's entry shape.
DELEGATE_EXPORTS: Final = (
    ("identity_delegateaccessevent", "id", "uuid", (), "organization"),
)
EXPORT_V5: Final = tuple(sorted((*EXPORT_V4, *DELEGATE_EXPORTS)))
EXCLUDED_V5: Final = EXCLUDED_V4

# Plain calls and comprehensions only, so the clock census decodes this SQL.
_BRANCHES_V5: Final = "\n".join(
    _sealed(
        _branch(
            relation,
            key,
            kind,
            omitted,
            scope,
            "IF" if relation == EXPORT_V5[0][0] else "ELSIF",
        ),
        relation,
    )
    for relation, key, kind, omitted, scope in EXPORT_V5
)
FORWARD: Final = _V1_READER.replace(_BRANCHES, _BRANCHES_V5)
REVERSE: Final = V4_READER
