"""Frozen export-v4 relation set: export v3 plus todo 68's RNDS submission log.

Export v3 (``_export_v3_sql.py``) is frozen, and a new relation needs a new export
version. Todo 68 added ``interop_rndssubmission`` after v3 was frozen. Each row is
the clinic's record of one laboratory-result document sent to the RNDS: the exact
sealed FHIR bundle (a tenant envelope, exported decrypted like every clinical
document), its digests, the RNDS answer with the identifier RNDS assigned, and the
result version, enrollment, actor and capability version it was sent under. The
identifier exists nowhere else and a later system needs it to correct a document
RNDS holds, so v4 exports the relation at organization scope with no omitted
column, as v1 exports the TISS submissions and the partner webhook deliveries.
Identity 0051, every app's last migration, applies this reader right after export
v3, so its populated-rollback refusal covers v4 too; clinic_resolver reads the
table through interop.0004's grant, so v4 changes no privilege. The reverse is
the v3 reader. This module never changes either.
"""

from typing import Final

from apps.interop.migrations._export_sql import _BRANCHES, _branch
from apps.interop.migrations._export_v2_sql import _V1_READER, _sealed
from apps.interop.migrations._export_v3_sql import EXCLUDED_V3, EXPORT_V3
from apps.interop.migrations._export_v3_sql import FORWARD as V3_READER

# The RNDS submission log (todo 68), in export v1's entry shape.
RNDS_EXPORTS: Final = (("interop_rndssubmission", "id", "uuid", (), "organization"),)
EXPORT_V4: Final = tuple(sorted((*EXPORT_V3, *RNDS_EXPORTS)))
EXCLUDED_V4: Final = EXCLUDED_V3

# Plain calls and comprehensions only, so the clock census decodes this SQL.
_BRANCHES_V4: Final = "\n".join(
    _sealed(
        _branch(
            relation,
            key,
            kind,
            omitted,
            scope,
            "IF" if relation == EXPORT_V4[0][0] else "ELSIF",
        ),
        relation,
    )
    for relation, key, kind, omitted, scope in EXPORT_V4
)
FORWARD: Final = _V1_READER.replace(_BRANCHES, _BRANCHES_V4)
REVERSE: Final = V3_READER
