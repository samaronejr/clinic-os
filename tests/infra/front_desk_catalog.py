"""Todo 54 front-desk resolver contract, from apps/ai/migrations/_front_desk_sql.py.

Appended as one block to the shared catalog registries. The agent bridges are
resolver-owned and executable by the runtime role; the table guard is
resolver-owned and executable by no other role once its triggers exist.
"""

from typing import Final

_AGENT_TABLES = ("ai_frontdeskthread", "ai_frontdeskturn")
TABLE_GRANTS: Final = {
    ("ai_knowledgearticle", "SELECT"),
    *(
        (table, privilege)
        for table in _AGENT_TABLES
        for privilege in ("SELECT", "INSERT", "UPDATE")
    ),
    # The challenge mints a booking-only channel grant; the session INSERT and
    # the reads of policy, conversation, messages and patient already existed.
    ("intake_patientaccessgrant", "INSERT"),
}
RESULTS: Final = {
    ("ai_front_desk_audit_scope", "requested_turn uuid"): (
        "TABLE(principal_id uuid, organization_id uuid, clinic_id uuid)"
    ),
    ("ai_front_desk_enqueue", "requested_message uuid"): "uuid",
    ("ai_front_desk_pending", ""): "SETOF uuid",
    ("ai_front_desk_turn", "requested_turn uuid, kek text"): (
        "TABLE(turn_id uuid, thread_id uuid, clinic_id uuid, channel text, "
        "enrollment_id uuid, thread_state text, verified boolean, session_id uuid, "
        "challenge_pending boolean, attempts integer, offered_appointment uuid, "
        "offered_practitioner uuid, offered_start timestamp with time zone, "
        "first_turn boolean, inbound_text text, policy_id uuid, tools text[], "
        "budget_left integer, offer_received boolean)"
    ),
    ("ai_front_desk_articles", "requested_turn uuid"): (
        "TABLE(article_id uuid, key text, version integer, title text, body text, "
        "keywords text[], link text)"
    ),
    ("ai_front_desk_verify", "requested_turn uuid, birth text, kek text"): (
        "TABLE(verified boolean, session_id uuid, exhausted boolean)"
    ),
    (
        "ai_front_desk_state",
        "requested_turn uuid, change text, appointment uuid, practitioner uuid, "
        "start timestamp with time zone, reason text",
    ): "void",
    (
        "ai_front_desk_finish",
        "requested_turn uuid, reply_body bytea, kek text, turn_intent text, "
        "turn_tool text, turn_outcome text, cited uuid",
    ): "uuid",
    ("ai_front_desk_guard", ""): "trigger",
}
_STABLE = frozenset({"ai_front_desk_pending", "ai_front_desk_articles"})
POSTURE: Final = {
    name: (
        ("v", False, ["clinic_resolver"])
        if name == "ai_front_desk_guard"
        else ("s" if name in _STABLE else "v", True, ["clinic_app", "clinic_resolver"])
    )
    for name, _arguments in RESULTS
}
POSTURE["ai_front_desk_audit_scope"] = (
    "v",
    True,
    ["clinic_app", "clinic_owner", "clinic_resolver"],
)
