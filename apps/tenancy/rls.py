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

# Posture registry declarations consumed by apps.tenancy.posture.
RLS_TARGETS: Final[frozenset[tuple[str, str]]] = TENANT_RLS_TARGETS
# tenancy_tenantdatakey carries bespoke resolver-only policies; the runtime
# role holds no grants on it at all.
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset({"tenancy_tenantdatakey"})
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
# tenancy_tenantprobe intentionally keeps full DML: it is the internal
# sentinel tests use to prove cross-tenant UPDATE/DELETE are blocked.
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "identity_organization": frozenset({"SELECT"}),
    "identity_clinic": frozenset({"SELECT"}),
    "identity_userclinicrole": frozenset({"SELECT"}),
    "tenancy_tenantprobe": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "tenancy_tenantdatakey": frozenset(),
}


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
