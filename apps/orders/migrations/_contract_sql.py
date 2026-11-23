"""Frozen contract SQL."""

from apps.orders.migrations import (
    _access_sql,
    _audit_sql,
    _guard_sql,
    _matching_sql,
    _patient_sql,
    _task_sql,
)

GRANTS = (
    "".join(
        f"GRANT SELECT ON clinic_app.{table} TO clinic_resolver;"
        for table in _access_sql.TABLES
    )
    + """
GRANT INSERT ON clinic_app.orders_resultmatch,clinic_app.orders_resultquarantine,
 clinic_app.orders_resultworkitem,clinic_app.workflows_task TO clinic_resolver;
"""
)
CONTRACT = _access_sql.render(
    GRANTS
    + _access_sql.FUNCTIONS
    + _guard_sql.FUNCTIONS
    + _task_sql.FUNCTIONS
    + _matching_sql.FUNCTIONS
    + _audit_sql.FUNCTIONS
    + _patient_sql.FUNCTIONS
    + _access_sql.POSTURE
    + _access_sql.POLICIES
    + _guard_sql.triggers(_access_sql.TABLES)
)
