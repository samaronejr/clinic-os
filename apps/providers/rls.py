"""Tenant posture declarations for the provider capability domain.

The lifecycle registry is platform-level operational data, not tenant
content: rows are written only by the ``clinic_owner`` role through the
``provider_capability`` management command and read by the runtime role
for the ``is_live`` gate. The tables deliberately carry no row-level
security policy; ``clinic_app`` holds SELECT only.
"""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset()
NON_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "providers_providercapability",
        "providers_capabilityversion",
        "providers_capabilityapproval",
        "providers_activationrecord",
        "providers_healthevent",
    }
)
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "providers_providercapability": frozenset({"SELECT"}),
    "providers_capabilityversion": frozenset({"SELECT"}),
    "providers_capabilityapproval": frozenset({"SELECT"}),
    "providers_activationrecord": frozenset({"SELECT"}),
    "providers_healthevent": frozenset({"SELECT"}),
}
# Column-level privileges held by the runtime role (pg_attribute.attacl).
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset()
