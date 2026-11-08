"""Exact analytics posture; only the refresh resolver writes aggregates."""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final = frozenset(
    {
        "analytics_metricdefinitionversion",
        "analytics_readmodelrefresh",
        "analytics_savedreport",
        "analytics_analystreview",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final = {
    table: frozenset(
        {"SELECT", "INSERT"}
        if table
        in {
            "analytics_savedreport",
            "analytics_analystreview",
        }
        else {"SELECT"}
    )
    for table in CUSTOM_RLS_TABLES
}
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset()
