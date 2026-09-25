"""Versioned organization-RLS helpers owned by the comms domain."""

from typing import Final

COMMS_RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("comms_integrationoperation", "organization_id")}
)
ALL_COMMS_RLS_TARGETS: Final = COMMS_RLS_TARGETS | {
    ("comms_appointmentreminder", "organization_id")
}

# Posture registry declarations consumed by apps.tenancy.posture.
RLS_TARGETS: Final[frozenset[tuple[str, str]]] = ALL_COMMS_RLS_TARGETS
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset()
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "comms_integrationoperation": frozenset({"SELECT", "INSERT"}),
    # Reminder snapshots are inserted only by the appointment trigger.
    "comms_appointmentreminder": frozenset({"SELECT"}),
}
# Column-level privileges held by the runtime role (pg_attribute.attacl).
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {
        ("comms_integrationoperation", "attempt_count", "UPDATE"),
        ("comms_integrationoperation", "last_callback_event_id", "UPDATE"),
        ("comms_integrationoperation", "last_error", "UPDATE"),
        ("comms_integrationoperation", "provider_reference", "UPDATE"),
        ("comms_integrationoperation", "status", "UPDATE"),
        ("comms_integrationoperation", "updated_at", "UPDATE"),
    }
)


def apply_comms_rls(table: str, tenant_column: str) -> str:
    """Build exact FORCE-RLS DDL for one versioned comms table."""
    if (table, tenant_column) not in COMMS_RLS_TARGETS:
        raise KeyError((table, tenant_column))
    condition = (
        f"{tenant_column} = "
        "NULLIF(pg_catalog.current_setting('app.current_tenant', true), '')"
        "::pg_catalog.uuid"
    )
    return f"""
ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinic_app.{table}
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING ({condition})
    WITH CHECK ({condition});
"""


def remove_comms_rls(table: str, tenant_column: str) -> str:
    """Build exact reverse DDL for one versioned comms table."""
    if (table, tenant_column) not in COMMS_RLS_TARGETS:
        raise KeyError((table, tenant_column))
    return f"""
DROP POLICY IF EXISTS tenant_isolation ON clinic_app.{table};
ALTER TABLE clinic_app.{table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} DISABLE ROW LEVEL SECURITY;
"""
