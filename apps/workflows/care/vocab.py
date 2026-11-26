"""Closed care vocabularies shared by models, validation and the SQL guard."""

from typing import Final

from django.utils.translation import gettext_noop, pgettext_lazy

ACTION_KINDS: Final = (
    "return_window",
    "order",
    "referral",
    "questionnaire",
    "monitoring_task",
    "patient_instruction",
    "staff_coordination",
)
# Administrative kinds are executed by staff; every other kind is a clinical
# decision and only a clinician may author or approve it.
# Display msgids for the closed kinds (rendered through gettext at use).
ACTION_KIND_LABELS: Final = {
    "return_window": gettext_noop("Return window"),
    "staff_coordination": gettext_noop("Staff coordination"),
    "order": gettext_noop("Order follow-up"),
    "questionnaire": gettext_noop("Questionnaire"),
    "monitoring_task": gettext_noop("Monitoring task"),
    "referral": gettext_noop("Referral"),
    "patient_instruction": gettext_noop("Patient instruction"),
}
ADMIN_KINDS: Final = frozenset({"return_window", "staff_coordination"})
CLINICAL_KINDS: Final = frozenset(ACTION_KINDS) - ADMIN_KINDS
# A patient-facing instruction is clinician-owned content, not staff work.
TASKLESS_KINDS: Final = frozenset({"patient_instruction"})
ACTION_STATES: Final = ("open", "assigned", "overdue", "done", "cancelled")
ACTION_STATE_LABELS: Final = {
    "open": pgettext_lazy("care action state", "Open"),
    "assigned": pgettext_lazy("care action state", "Assigned"),
    "overdue": pgettext_lazy("care action state", "Overdue"),
    "done": pgettext_lazy("care action state", "Done"),
    "cancelled": pgettext_lazy("care action state", "Cancelled"),
}
REVIEW_STATES: Final = ("none", "required", "confirmed")
PLAN_STATES: Final = ("active", "superseded")
PLAN_STATE_LABELS: Final = {
    "active": pgettext_lazy("care plan state", "Active"),
    "superseded": pgettext_lazy("care plan state", "Superseded"),
}
REFERRAL_TARGETS: Final = ("specialty", "external", "unspecified")
REFERRAL_STATUSES: Final = (
    "requested",
    "scheduled",
    "completed",
    "cancelled",
    "documented",
)
RECALL_REASONS: Final = ("routine_return", "test_follow_up", "chronic_review")
RECALL_STATES: Final = ("scheduled", "materialized")
PRO_INSTRUMENTS: Final = ("phq9", "gad7")
# Response queues belong to roles that may read the response for their own
# patients (RP matrix: clinical narrative and scope-limited observations).
PRO_RESPONSE_ROLES: Final = ("physician", "nurse", "allied_professional")
PRO_ASSIGNMENT_STATES: Final = ("active", "completed")
PRO_OCCURRENCE_STATES: Final = ("pending", "responded", "no_response")
OUTCOMES: Final = ("reviewed", "resolved", "escalated")
MAX_WINDOW_DAYS: Final = 730
MAX_RETURN_WEEKS: Final = 104
MAX_RECALL_DAYS: Final = 730
MAX_PRO_INTERVAL_DAYS: Final = 365
MAX_PRO_OCCURRENCES: Final = 12
NO_RESPONSE_GRACE_DAYS: Final = 3
# The care scanner: one job per due record, at most SCAN_PER_TENANT records of
# one organization per scan, SCAN_LIMIT in total, oldest first. A record its
# stored actor could not process is recorded and re-listed after the delay.
SCAN_KINDS: Final = ("recall", "overdue", "pro_missed", "stale")
SCAN_PER_TENANT: Final = 25
SCAN_LIMIT: Final = 500
SCAN_RETRY_MINUTES: Final = 60
MAX_INSTRUCTION: Final = 2000
MAX_LABEL: Final = 200
POLICY_REF_PATTERN: Final = r"^[a-z][a-z0-9-]{2,62}$"
