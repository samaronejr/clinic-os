"""Todo 55 coordination resolver contract, from _coordination_sql.py.

Appended as one block to the shared catalog registries. ``workflows_urgent_audit``
(the patient-session audit binding) and the coordination table guard are owned
by ``clinic_owner``, as the portal message binding and guard are, so they sit
outside this resolver-owned set.
"""

from typing import Final

# The patient bridge opens concerns; the outcome bridge marks failed deliveries.
# The reads of messages, conversations and clinic roles already existed.
TABLE_GRANTS: Final = {
    ("workflows_urgentcoveragepolicy", "SELECT"),
    ("workflows_urgentconcern", "SELECT"),
    ("workflows_urgentconcern", "INSERT"),
    ("workflows_releasedelivery", "SELECT"),
}
COLUMN_GRANTS: Final = {
    ("workflows_releasedelivery", column, "UPDATE")
    for column in ("failure_reason", "failure_task_id")
}
RESULTS: Final = {
    ("workflows_urgent_responsible", "clinic uuid, roles character varying[]"): (
        "boolean"
    ),
    ("workflows_coverage_policy", "tenant uuid, clinic uuid"): "uuid",
    ("workflows_report_urgent", "requested_message uuid"): "uuid",
    ("workflows_patient_coverage", ""): (
        "TABLE(weekdays smallint[], opens_at time without time zone, "
        "closes_at time without time zone, response_hours smallint)"
    ),
    ("workflows_urgent_audit_scope", "record_id uuid"): (
        "TABLE(session_id uuid, organization_id uuid, clinic_id uuid)"
    ),
    ("workflows_release_send_eligible", "requested_message uuid"): "boolean",
    ("workflows_release_system_task_valid", "row_data jsonb"): "boolean",
    ("workflows_release_outcome", ""): "trigger",
}
# (volatility, executable by clinic_app, EXECUTE grantees)
POSTURE: Final = {
    "workflows_urgent_responsible": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "workflows_coverage_policy": ("s", False, ["clinic_resolver"]),
    "workflows_report_urgent": ("v", True, ["clinic_app", "clinic_resolver"]),
    "workflows_patient_coverage": ("s", True, ["clinic_app", "clinic_resolver"]),
    "workflows_urgent_audit_scope": ("s", False, ["clinic_owner", "clinic_resolver"]),
    "workflows_release_send_eligible": ("v", True, ["clinic_app", "clinic_resolver"]),
    "workflows_release_system_task_valid": ("s", False, ["clinic_resolver"]),
    "workflows_release_outcome": ("v", False, ["clinic_owner", "clinic_resolver"]),
}
