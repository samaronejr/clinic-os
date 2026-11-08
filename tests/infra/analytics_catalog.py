"""Exact analytics catalog captured from the migrated PostgreSQL 16 catalog."""

from typing import Final

SOURCE_VIEW_DIGESTS: Final = {
    "analytics_metricfact": (
        "d0e3c23e1f92765ebccc7b59af0feca6da548716051ccb986fca33949b8ea48a"
    ),
}

TABLE_GRANTS: Final = {
    ("analytics_analystreview", "SELECT"),
    ("analytics_metricdefinitionversion", "INSERT"),
    ("analytics_metricdefinitionversion", "SELECT"),
    ("analytics_readmodelrefresh", "INSERT"),
    ("analytics_readmodelrefresh", "SELECT"),
    ("analytics_savedreport", "SELECT"),
}
COLUMN_GRANTS: Final = {
    ("audit_event_tenant", "actor_user_id", "SELECT"),
    ("audit_event_tenant", "event_type", "SELECT"),
    ("audit_event_tenant", "occurred_at_utc", "SELECT"),
    ("audit_event_tenant", "organization_id", "SELECT"),
    ("audit_event_tenant", "payload", "SELECT"),
}
RESULTS: Final = {
    ("analytics_allowed", "requested_clinic uuid, metric text"): "boolean",
    ("analytics_authorization", "requested_clinic uuid"): "text",
    ("analytics_guard", ""): "trigger",
    (
        "analytics_refresh",
        "requested_clinic uuid, query jsonb, command uuid, "
        "window_start timestamp with time zone, window_end timestamp with time zone",
    ): "TABLE(refresh_id uuid, changed boolean, registered boolean)",
    (
        "analytics_validate",
        "requested_clinic uuid, query jsonb, "
        "window_start timestamp with time zone, window_end timestamp with time zone",
    ): "void",
    ("analytics_workflow_receipt", ""): "trigger",
}
POSTURE: Final = {
    "analytics_allowed": ("v", True, ["clinic_app", "clinic_resolver"]),
    "analytics_authorization": ("v", True, ["clinic_app", "clinic_resolver"]),
    "analytics_guard": ("v", False, ["clinic_resolver"]),
    "analytics_refresh": ("v", True, ["clinic_app", "clinic_resolver"]),
    "analytics_validate": ("v", False, ["clinic_resolver"]),
    "analytics_workflow_receipt": ("v", False, ["clinic_resolver"]),
}
