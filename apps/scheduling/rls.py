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

# Posture registry declarations consumed by apps.tenancy.posture.
RLS_TARGETS: Final[frozenset[tuple[str, str]]] = ALL_SCHEDULING_RLS_TARGETS
# Booking events and waitlist tables carry bespoke patient/staff policies
# declared in apps/scheduling/migrations/_patient_booking_sql.py and
# _waitlist_sql.py.
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "scheduling_patientbookingevent",
        "scheduling_waitlistentry",
        "scheduling_waitlistoffer",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "scheduling_availabilityblock": frozenset({"SELECT", "INSERT"}),
    "scheduling_appointment": frozenset({"SELECT", "INSERT"}),
    "scheduling_patientbookingevent": frozenset({"SELECT"}),
    "scheduling_waitlistentry": frozenset({"SELECT", "INSERT"}),
    "scheduling_waitlistoffer": frozenset({"SELECT", "INSERT"}),
}
# Column-level privileges held by the runtime role (pg_attribute.attacl).
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {
        ("scheduling_appointment", "cancellation_reason", "UPDATE"),
        ("scheduling_appointment", "cancelled_at", "UPDATE"),
        ("scheduling_appointment", "end_at", "UPDATE"),
        ("scheduling_appointment", "start_at", "UPDATE"),
        ("scheduling_appointment", "status", "UPDATE"),
        ("scheduling_appointment", "updated_at", "UPDATE"),
        ("scheduling_availabilityblock", "retired_at", "UPDATE"),
        ("scheduling_availabilityblock", "updated_at", "UPDATE"),
        ("scheduling_waitlistentry", "state", "UPDATE"),
        ("scheduling_waitlistoffer", "appointment_id", "UPDATE"),
        ("scheduling_waitlistoffer", "responded_at", "UPDATE"),
        ("scheduling_waitlistoffer", "state", "UPDATE"),
    }
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
