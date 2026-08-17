"""Versioned organization-RLS helpers owned by the scheduling domain."""

from typing import Final

SCHEDULING_RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("scheduling_availabilityblock", "organization_id")}
)
APPOINTMENT_RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("scheduling_appointment", "organization_id")}
)
ALL_SCHEDULING_RLS_TARGETS: Final[frozenset[tuple[str, str]]] = (
    SCHEDULING_RLS_TARGETS | APPOINTMENT_RLS_TARGETS
)


def apply_scheduling_rls(table: str, tenant_column: str) -> str:
    """Build exact FORCE-RLS DDL for one versioned scheduling table."""
    if (table, tenant_column) not in ALL_SCHEDULING_RLS_TARGETS:
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


def remove_scheduling_rls(table: str, tenant_column: str) -> str:
    """Build exact reverse DDL for one versioned scheduling table."""
    if (table, tenant_column) not in ALL_SCHEDULING_RLS_TARGETS:
        raise KeyError((table, tenant_column))
    return f"""
DROP POLICY IF EXISTS tenant_isolation ON clinic_app.{table};
ALTER TABLE clinic_app.{table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} DISABLE ROW LEVEL SECURITY;
"""
