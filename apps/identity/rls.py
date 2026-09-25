"""Tenant posture declarations for the identity domain.

The foundation tables ``identity_organization``, ``identity_clinic`` and
``identity_userclinicrole`` are declared in ``apps.tenancy.rls`` because the
tenancy migration that installs the shared ``tenant_isolation`` policy owns
them. This module declares the remaining identity tables.
"""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
# Clinic configuration, per-user preferences and physician verification
# rows carry bespoke policies declared in apps/identity/migrations/0007,
# 0009 and 0011.
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "identity_clinicconfiguration",
        "identity_rolegrant",
        "identity_careteammembership",
        "identity_professionalregistration",
        "identity_physicianprofile",
        "identity_physicianevidence",
        "identity_userpreference",
    }
)
# identity_user is reachable only through resolver-owned functions; the
# runtime role holds no grants on it.
NON_RLS_TABLES: Final[frozenset[str]] = frozenset({"identity_user"})
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "identity_user": frozenset(),
    "identity_rolegrant": frozenset({"SELECT"}),
    "identity_careteammembership": frozenset({"SELECT"}),
    "identity_professionalregistration": frozenset({"SELECT"}),
    "identity_clinicconfiguration": frozenset({"SELECT", "INSERT"}),
    "identity_physicianprofile": frozenset({"SELECT"}),
    "identity_physicianevidence": frozenset({"SELECT", "INSERT"}),
    "identity_userpreference": frozenset({"SELECT", "INSERT"}),
}
# New authorization tables have no runtime column writes: provisioning remains
# owner-controlled. Column grants below belong only to legacy physician checks.
# Column-level privileges held by the runtime role (pg_attribute.attacl).
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {
        ("identity_physicianprofile", "expires_at", "UPDATE"),
        ("identity_physicianprofile", "last_checked_at", "UPDATE"),
        ("identity_physicianprofile", "recheck_at", "UPDATE"),
        ("identity_physicianprofile", "status", "UPDATE"),
        ("identity_userpreference", "density", "UPDATE"),
        ("identity_userpreference", "theme", "UPDATE"),
        ("identity_userpreference", "updated_at", "UPDATE"),
    }
)
