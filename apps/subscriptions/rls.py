"""Exact FORCE-RLS and resolver-only mutation declarations."""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
TENANT_TABLES: Final = frozenset(
    {
        "subscriptions_subscription",
        "subscriptions_subscriptionevent",
        "subscriptions_seat",
        "subscriptions_usageevent",
        "subscriptions_usagerollup",
    }
)
PLATFORM_TABLES: Final = frozenset({"subscriptions_plan", "subscriptions_entitlement"})
CUSTOM_RLS_TABLES: Final = TENANT_TABLES | PLATFORM_TABLES
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final = {table: frozenset({"SELECT"}) for table in CUSTOM_RLS_TABLES}
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset()
