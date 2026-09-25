"""Tenant posture declarations for the retention domain.

Retention tables carry bespoke ``policy_*``, ``hold_*``, ``release_*`` and
``export_*`` policies declared in
``apps/retention/migrations/_retention_sql.py``; none uses the shared
``tenant_isolation`` policy.
"""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "retention_retentionpolicy",
        "retention_legalhold",
        "retention_recordrelease",
        "retention_recordexport",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "retention_retentionpolicy": frozenset({"SELECT", "INSERT"}),
    "retention_legalhold": frozenset({"SELECT", "INSERT"}),
    "retention_recordrelease": frozenset({"SELECT", "INSERT"}),
    "retention_recordexport": frozenset({"SELECT", "INSERT"}),
}
# Column-level privileges held by the runtime role (pg_attribute.attacl).
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {
        ("retention_legalhold", "release_authority", "UPDATE"),
        ("retention_legalhold", "release_reason", "UPDATE"),
        ("retention_legalhold", "released_at", "UPDATE"),
        ("retention_legalhold", "released_by_id", "UPDATE"),
        ("retention_recordrelease", "revoked_at", "UPDATE"),
        ("retention_recordrelease", "revoked_by_id", "UPDATE"),
        ("retention_retentionpolicy", "approved_at", "UPDATE"),
        ("retention_retentionpolicy", "approved_by_id", "UPDATE"),
        ("retention_retentionpolicy", "retired_at", "UPDATE"),
        ("retention_retentionpolicy", "retired_by_id", "UPDATE"),
        ("retention_retentionpolicy", "state", "UPDATE"),
    }
)
