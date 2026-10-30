"""Todo 51 resolver contract, derived from apps/comms/migrations/_inbox_sql.py.

Append as one block to the shared catalog registries. Confirm against the live
catalog in the database phase; `comms_ingest_audit` and the trigger guards are
owned by `clinic_owner`, so they sit outside this resolver-owned set.
"""

from typing import Final

TABLE_GRANTS: Final = {
    ("comms_provideraccount", "SELECT"),
    ("comms_conversation", "SELECT"),
    ("comms_message", "SELECT"),
    ("comms_templateversion", "SELECT"),
}
# The only writes the resolver makes: the definer that stamps the service window.
COLUMN_GRANTS: Final = {
    ("comms_conversation", "last_inbound_at", "UPDATE"),
    ("comms_conversation", "last_activity_at", "UPDATE"),
}
RESULTS: Final = {
    ("comms_resolve_inbound", "account uuid, proof text"): (
        "TABLE(organization_id uuid, clinic_id uuid, principal_id uuid, "
        "channel text, provider text, external_account_id text)"
    ),
    ("comms_ingest_account", "clinic uuid, channel_name text"): "uuid",
    ("comms_open_window", "conversation uuid"): "void",
    ("comms_audit_scope", "record_id uuid, event_name text"): (
        "TABLE(principal_id uuid, organization_id uuid, clinic_id uuid)"
    ),
}
POSTURE: Final = {
    name: (
        "v",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"]
        if name == "comms_audit_scope"
        else ["clinic_app", "clinic_resolver"],
    )
    for name, _arguments in RESULTS
}
