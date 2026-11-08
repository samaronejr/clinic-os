"""Todo 50 portal messages resolver contract, from _portal_messages_sql.py.

Appended as one block to the shared catalog registries. ``comms_patient_audit``
and the message guard are owned by ``clinic_owner``, so they sit outside this
resolver-owned set.
"""

from typing import Final

# The reply resolver writes the inbound row; todo 51 already granted SELECT.
TABLE_GRANTS: Final = {("comms_message", "INSERT")}
COLUMN_GRANTS: Final = {
    ("comms_conversation", column, "UPDATE")
    for column in (
        "state",
        "needs_clinician",
        "triage_category",
        "revision",
        "updated_at",
    )
}
_SCOPE = "TABLE(session_id uuid, organization_id uuid, clinic_id uuid)"
RESULTS: Final = {
    ("comms_patient_scope", ""): (
        "TABLE(session_id uuid, organization_id uuid, clinic_id uuid, "
        "clinic_timezone text, acting_for_patient boolean)"
    ),
    ("comms_patient_conversations", ""): (
        "TABLE(conversation_id uuid, purpose text, state text, "
        "created_at timestamp with time zone, "
        "last_message_at timestamp with time zone, message_count integer)"
    ),
    ("comms_patient_messages", "requested_conversation uuid, kek text"): (
        "TABLE(message_id uuid, from_clinic boolean, body text, "
        "created_at timestamp with time zone)"
    ),
    (
        "comms_patient_reply",
        "requested_clinic uuid, requested_conversation uuid, body_envelope bytea, "
        "reply_key uuid, kek text, triage text",
    ): (
        "TABLE(message_id uuid, conversation_id uuid, changed boolean, "
        "escalated boolean)"
    ),
    ("comms_patient_audit_scope", "record_id uuid, event_name text"): _SCOPE,
}
POSTURE: Final = {
    name: (
        "v" if name == "comms_patient_reply" else "s",
        name != "comms_patient_audit_scope",
        ["clinic_owner", "clinic_resolver"]
        if name == "comms_patient_audit_scope"
        else ["clinic_app", "clinic_resolver"],
    )
    for name, _arguments in RESULTS
}
