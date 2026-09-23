"""Versioned organization-RLS helpers owned by the comms domain."""

from typing import Final

COMMS_RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset(
    {("comms_integrationoperation", "organization_id")}
)
ALL_COMMS_RLS_TARGETS: Final = COMMS_RLS_TARGETS | {
    ("comms_appointmentreminder", "organization_id")
}


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
