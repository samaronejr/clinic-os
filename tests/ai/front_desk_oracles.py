"""Bind catalog-discovered front-desk protocols to real authority tests (todo 54)."""

from ai import test_front_desk, test_front_desk_guards, test_front_desk_views

CASES = {
    case.__name__: case
    for case in (
        test_front_desk.test_unverified_identity_cannot_learn_appointment_details,
        test_front_desk.test_booking_happens_only_after_explicit_acceptance_of_the_offer,
        test_front_desk.test_no_availability_offers_the_existing_waitlist,
        test_front_desk.test_urgency_routes_to_a_human_with_fixed_safety_text,
        test_front_desk.test_missing_answer_says_it_does_not_know_and_forwards,
        test_front_desk.test_injected_commands_never_become_tool_calls,
        test_front_desk.test_the_agent_needs_both_opt_ins_and_the_policy_tools,
        test_front_desk.test_concurrent_acceptances_book_the_offer_once,
        test_front_desk_views.test_knowledge_page_drafts_approves_and_retires_exact_versions,
        test_front_desk_views.test_handoff_page_lists_reasons_and_resolves_by_post_body,
        test_front_desk_guards.test_database_guards_refuse_raw_front_desk_forgery,
        test_front_desk_guards.test_front_desk_sql_rechecks_inactive_and_foreign_actor,
    )
}
SQL_PROTOCOL_ORACLES = {
    name: list(CASES)
    for name in (
        "clinic_app.ai_front_desk_articles",
        "clinic_app.ai_front_desk_enqueue",
        "clinic_app.ai_front_desk_finish",
        "clinic_app.ai_front_desk_guard",
        "clinic_app.ai_front_desk_pending",
        "clinic_app.ai_front_desk_state",
        "clinic_app.ai_front_desk_turn",
        "clinic_app.ai_front_desk_verify",
    )
}
ACTOR_PROTOCOL_ORACLES = {
    "clinic_app.ai_front_desk_guard()": (
        "ai.test_front_desk_guards.test_front_desk_sql_rechecks_inactive_and_foreign_actor",
    ),
}
