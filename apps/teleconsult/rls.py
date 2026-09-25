"""Tenant posture declarations for the teleconsult domain.

Teleconsult tables carry bespoke ``teleconsult_*`` policies declared in
``apps/teleconsult/migrations/_teleconsult_sql.py``; none uses the shared
``tenant_isolation`` policy.
"""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "teleconsult_teleconsultsession",
        "teleconsult_teleconsultroom",
        "teleconsult_teleconsultcredential",
        "teleconsult_teleconsultevent",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "teleconsult_teleconsultsession": frozenset({"SELECT", "INSERT"}),
    "teleconsult_teleconsultroom": frozenset({"SELECT", "INSERT"}),
    "teleconsult_teleconsultcredential": frozenset({"SELECT", "INSERT"}),
    "teleconsult_teleconsultevent": frozenset({"SELECT", "INSERT"}),
}
