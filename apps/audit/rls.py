"""Tenant posture declarations for the audit domain.

``audit_event`` is an append-only ledger reachable only through the
resolver-owned ``audit_append`` function; it deliberately has no row-level
security policy and no runtime-role grants.
"""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset()
NON_RLS_TABLES: Final[frozenset[str]] = frozenset({"audit_event"})
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "audit_event": frozenset(),
}
