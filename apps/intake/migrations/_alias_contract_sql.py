"""Frozen alias contract SQL."""

from apps.intake.migrations._alias_bindings_sql import (
    SQL as BINDINGS_SQL,
)
from apps.intake.migrations._alias_bindings_sql import composite_bindings
from apps.intake.migrations._alias_guard_sql import FINALIZE_SQL
from apps.intake.migrations._alias_guard_sql import SQL as GUARD_SQL
from apps.intake.migrations._alias_scope_sql import SQL as SCOPE_SQL
from apps.intake.rls import apply_intake_rls
from apps.tenancy.migrations_support import revoke_default_dml

TABLES = (
    "intake_mergedecision",
    "intake_patientalias",
    "intake_patientmergetransfer",
)
ALIAS_SQL = (
    "".join(
        revoke_default_dml(table, ("SELECT", "INSERT"))
        + apply_intake_rls(table, "organization_id")
        for table in TABLES
    )
    + SCOPE_SQL
    + GUARD_SQL
    + BINDINGS_SQL
    + composite_bindings()
    + FINALIZE_SQL
)
