"""Frozen SQL for matched patient authority."""

from ._access_sql import (
    CURRENT_EVENT_RECEIPT_SQL,
    CURRENT_INGEST_SCOPE_SQL,
    CURRENT_RESULT_READ_SQL,
    render,
)
from ._guard_sql import CURRENT_GUARD_SQL

SQL = (
    "SET LOCAL ROLE clinic_resolver;"
    + render(CURRENT_RESULT_READ_SQL)
    + render(CURRENT_INGEST_SCOPE_SQL)
    + render(CURRENT_EVENT_RECEIPT_SQL)
    + render(CURRENT_GUARD_SQL)
    + "RESET ROLE;"
)
