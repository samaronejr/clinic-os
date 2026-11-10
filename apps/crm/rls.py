"""Exact runtime CRM grants; managers never write patient choices or scores."""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final = frozenset(
    {
        "crm_referralsource",
        "crm_lead",
        "crm_marketingpreference",
        "crm_campaign",
        "crm_campaignsend",
        "crm_satisfactionsurvey",
        "crm_publicbookingprofile",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final = {
    table: frozenset({"SELECT", "INSERT"}) for table in CUSTOM_RLS_TABLES
}
COLUMN_GRANTS: Final = frozenset(
    (table, column, "UPDATE")
    for table, columns in {
        "crm_lead": ("state", "enrollment_id", "name", "email"),
        "crm_campaign": ("state", "revision", "excluded_count"),
        "crm_publicbookingprofile": ("active", "description"),
    }.items()
    for column in columns
)
