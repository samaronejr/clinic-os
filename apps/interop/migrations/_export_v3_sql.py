"""Frozen export-v3 relation set: export v2 plus identity 0051's enterprise relations.

Export v2 (``_export_v2_sql.py``) is frozen, and a new relation needs a new export
version. Identity 0051 (todo 65) added ten domain relations after v2 was frozen.
Version 3 is v2 plus those ten: the SCIM bearer tokens (stored as digests) are
excluded as credentials, like every other authentication secret, and the other
nine are exported. ``identity_scimresource`` is exported without the digest of
the identity provider's external id and the directory search index, like the
other digests and blind indexes. Identity 0051 applies this reader right after it
creates the relations, so 0051 stays every app's last migration and its
populated-rollback refusal covers v3 too; clinic_resolver already reads all ten
through 0051's grants, so v3 changes no privilege. The reverse is the v2 reader.
This module never changes either.
"""

from typing import Final

from apps.interop.migrations._export_sql import _BRANCHES, _branch
from apps.interop.migrations._export_v2_sql import (
    _V1_READER,
    EXCLUDED_V2,
    EXPORT_V2,
    _sealed,
)
from apps.interop.migrations._export_v2_sql import FORWARD as V2_READER

# Identity 0051's relations, in export v1's entry shape.
ENTERPRISE_EXPORTS: Final = (
    ("identity_breakglassgrant", "id", "uuid", (), "organization"),
    ("identity_governancenotice", "id", "uuid", (), "organization"),
    ("identity_scimmembership", "id", "uuid", (), "organization"),
    (
        "identity_scimresource",
        "id",
        "uuid",
        ("external_hash", "search_hash"),
        "organization",
    ),
    ("identity_scimrolebinding", "id", "uuid", (), "organization"),
    ("identity_scimrolemapping", "id", "uuid", (), "organization"),
    ("identity_ssoconnection", "id", "uuid", (), "organization"),
    ("identity_supportaccessgrant", "id", "uuid", (), "organization"),
    ("identity_unitgovernancechange", "id", "uuid", (), "organization"),
)
ENTERPRISE_EXCLUSIONS: Final = {"identity_scimtoken": "credential"}
EXPORT_V3: Final = tuple(sorted((*EXPORT_V2, *ENTERPRISE_EXPORTS)))
EXCLUDED_V3: Final = EXCLUDED_V2 | ENTERPRISE_EXCLUSIONS

# Plain calls and comprehensions only, so the clock census decodes this SQL.
_BRANCHES_V3: Final = "\n".join(
    _sealed(
        _branch(
            relation,
            key,
            kind,
            omitted,
            scope,
            "IF" if relation == EXPORT_V3[0][0] else "ELSIF",
        ),
        relation,
    )
    for relation, key, kind, omitted, scope in EXPORT_V3
)
FORWARD: Final = _V1_READER.replace(_BRANCHES, _BRANCHES_V3)
REVERSE: Final = V2_READER
