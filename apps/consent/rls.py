"""Tenant posture declarations for the consent domain.

Consent tables carry bespoke ``consent_*`` policies declared in
``apps/consent/migrations/_consent_sql.py``; none uses the shared
``tenant_isolation`` policy.
"""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "consent_consenttext",
        "consent_consentacceptance",
        "consent_consentrevocation",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "consent_consenttext": frozenset({"SELECT", "INSERT"}),
    "consent_consentacceptance": frozenset({"SELECT", "INSERT"}),
    "consent_consentrevocation": frozenset({"SELECT", "INSERT"}),
}
