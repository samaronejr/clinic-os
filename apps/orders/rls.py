"""Exact least-privilege orders posture; machine principals get no table grants."""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final = frozenset(
    {
        "orders_labaccount",
        "orders_order",
        "orders_orderitem",
        "orders_orderevent",
        "orders_result",
        "orders_resultversion",
        "orders_resultobservation",
        "orders_resultmatch",
        "orders_resultquarantine",
        "orders_resultrejection",
        "orders_resultacknowledgment",
        "orders_resultrelease",
        "orders_resultworkitem",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
_INSERTS: Final = frozenset(
    {
        "orders_order",
        "orders_orderitem",
        "orders_orderevent",
        "orders_result",
        "orders_resultversion",
        "orders_resultobservation",
        "orders_resultrejection",
        "orders_resultacknowledgment",
        "orders_resultrelease",
    }
)
RUNTIME_GRANTS: Final = {
    table: frozenset({"SELECT", "INSERT"} if table in _INSERTS else {"SELECT"})
    for table in CUSTOM_RLS_TABLES
}
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset()
