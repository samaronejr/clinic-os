"""Bind the discovered analytics functions to executable database tests."""

from analytics import test_metrics, test_posture, test_workflow

VIEW_ORACLES = {
    "analytics_metricfact": test_posture.test_sql_reporting_boundary_matrix,
}

CASES = {
    case.__name__: case
    for case in (
        test_posture.test_sql_reporting_boundary_matrix,
        test_posture.test_saved_report_trigger_rejects_forged_scope_and_owner_history_is_immutable,
        test_posture.test_saved_report_trigger_itself_rejects_an_inactive_actor,
        test_metrics.test_cache_key_changes_with_effective_authorization_version,
        test_workflow.test_refresh_step_requires_its_exact_effect_and_replays_once,
    )
}
SQL_PROTOCOL_ORACLES = {
    "clinic_app.analytics_workflow_receipt": [
        "test_refresh_step_requires_its_exact_effect_and_replays_once",
    ],
    "clinic_app.analytics_allowed": ["test_sql_reporting_boundary_matrix"],
    "clinic_app.analytics_authorization": [
        "test_sql_reporting_boundary_matrix",
        "test_cache_key_changes_with_effective_authorization_version",
    ],
    "clinic_app.analytics_validate": ["test_sql_reporting_boundary_matrix"],
    "clinic_app.analytics_refresh": ["test_sql_reporting_boundary_matrix"],
    "clinic_app.analytics_guard": [
        "test_saved_report_trigger_rejects_forged_scope_and_owner_history_is_immutable",
        "test_saved_report_trigger_itself_rejects_an_inactive_actor",
    ],
}
ACTOR_PROTOCOL_ORACLES = {
    "clinic_app.analytics_authorization(uuid)": (
        "analytics.test_posture.test_sql_reporting_boundary_matrix",
    ),
    "clinic_app.analytics_refresh(uuid,jsonb,uuid,timestamp with time zone,"
    "timestamp with time zone)": (
        "analytics.test_posture.test_sql_reporting_boundary_matrix",
    ),
    "clinic_app.analytics_guard()": (
        "analytics.test_posture.test_saved_report_trigger_itself_rejects_an_inactive_actor",
    ),
}
