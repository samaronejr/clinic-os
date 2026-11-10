"""Frozen export-v2 relation set: export v1 plus the governed-import relations.

Export v1 (``_export_sql.py``) is frozen, and a new relation needs a new export
version. The governed import (todo 66) added seven domain relations that did not
exist when v1 was frozen. Version 2 is v1 plus those seven: six are exported and
the commit-batch work units are excluded as machinery. An ``interop_importfile``
row of role ``source`` is exported without ``data``: the export never reads a
stored import source, which todo 66 confines to the pipeline of an in-flight job.
Attachment rows carry their bytes like every clinical document. The reader keeps
one static branch per exported relation; this module never changes either.
"""

from typing import Final

from apps.interop.migrations._export_sql import (
    _BRANCHES,
    EXCLUDED_V1,
    EXPORT_V1,
    RESOLVER_READ_GRANTS,
    SQL,
    _branch,
)

# The governed import's relations, in export v1's entry shape.
IMPORT_EXPORTS: Final = (
    ("interop_hiddenenrollment", "id", "uuid", (), "organization"),
    ("interop_importedrecord", "id", "uuid", (), "organization"),
    ("interop_importfile", "id", "uuid", (), "organization"),
    ("interop_importjob", "id", "uuid", (), "organization"),
    ("interop_importmapping", "id", "uuid", (), "organization"),
    ("interop_importtransition", "id", "uuid", (), "organization"),
)
IMPORT_EXCLUSIONS: Final = {"interop_importbatch": "machinery"}
EXPORT_V2: Final = tuple(sorted((*EXPORT_V1, *IMPORT_EXPORTS)))
EXCLUDED_V2: Final = EXCLUDED_V1 | IMPORT_EXCLUSIONS
SEALED_SOURCE: Final = (
    "CASE WHEN t.role='source' THEN to_jsonb(t)-'data' ELSE to_jsonb(t) END"
)


def _sealed(branch: str, relation: str) -> str:
    return (
        branch.replace("t.id::text,to_jsonb(t)\n", f"t.id::text,{SEALED_SOURCE}\n")
        if relation == "interop_importfile"
        else branch
    )


# Plain calls and comprehensions only, so the clock census decodes this SQL.
_BRANCHES_V2: Final = "\n".join(
    _sealed(
        _branch(
            relation,
            key,
            kind,
            omitted,
            scope,
            "IF" if relation == EXPORT_V2[0][0] else "ELSIF",
        ),
        relation,
    )
    for relation, key, kind, omitted, scope in EXPORT_V2
)
# Export v1's reader without its one-time grants (interop.0002 keeps them),
# created in place so its owner, privileges and callers stay unchanged.
_V1_READER: Final = SQL.replace(
    "GRANT SELECT ON "
    + ",".join(f"clinic_app.{relation}" for relation in RESOLVER_READ_GRANTS)
    + " TO clinic_resolver;\n",
    "",
).replace(
    "CREATE FUNCTION clinic_app.interop_export_rows(",
    "CREATE OR REPLACE FUNCTION clinic_app.interop_export_rows(",
)
FORWARD: Final = _V1_READER.replace(_BRANCHES, _BRANCHES_V2)
REVERSE: Final = _V1_READER
