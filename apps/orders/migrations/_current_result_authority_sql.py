"""Frozen SQL for current result authority."""

from ._access_sql import CURRENT_RESULT_READ_SQL, render

SQL = (
    "SET LOCAL ROLE clinic_resolver;" + render(CURRENT_RESULT_READ_SQL) + "RESET ROLE;"
)
