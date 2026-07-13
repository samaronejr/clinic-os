"""PostgreSQL row-level security migration helpers."""

from typing import Final

TENANT_RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("identity_organization", "id"),
        ("identity_clinic", "organization_id"),
        ("identity_userclinicrole", "organization_id"),
        ("tenancy_tenantprobe", "organization_id"),
    }
)


def apply_tenant_rls(table: str, tenant_col: str) -> str:
    """Build fixed-identifier RLS DDL for one approved tenant table."""
    if (table, tenant_col) not in TENANT_RLS_TARGETS:
        raise KeyError((table, tenant_col))

    tenant_condition = (
        f"{tenant_col} = "
        "NULLIF(pg_catalog.current_setting('app.current_tenant', true), '')"
        "::pg_catalog.uuid"
    )
    return f"""
ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinic_app.{table}
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING ({tenant_condition})
    WITH CHECK ({tenant_condition});
"""


def remove_tenant_rls(table: str, tenant_col: str) -> str:
    """Build fixed-identifier reverse DDL for one approved tenant table."""
    if (table, tenant_col) not in TENANT_RLS_TARGETS:
        raise KeyError((table, tenant_col))

    return f"""
DROP POLICY IF EXISTS tenant_isolation ON clinic_app.{table};
ALTER TABLE clinic_app.{table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} DISABLE ROW LEVEL SECURITY;
"""
