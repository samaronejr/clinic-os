"""Todo 28 catalog pins derived from the migrated live PostgreSQL closure."""

from typing import Final

TABLE_GRANTS: Final = {
    ("ehr_medicationstatement", "SELECT"),
    ("ehr_observation", "SELECT"),
    ("ehr_restrictednote", "SELECT"),
    ("ehr_noteaccessgrant", "SELECT"),
    ("ehr_copiednoteblock", "SELECT"),
    ("ehr_timelinerefresh", "SELECT"),
    ("ehr_timelinerefresh", "INSERT"),
    ("ehr_timelineprojection", "SELECT"),
    ("ehr_timelineprojection", "INSERT"),
    ("ehr_episodeencounter", "SELECT"),
}
COLUMN_GRANTS: Final = {
    ("ehr_noteaccessgrant", "revoked_at", "UPDATE"),
    ("ehr_timelinerefresh", "revision", "UPDATE"),
    ("ehr_timelinerefresh", "refreshed_revision", "UPDATE"),
    ("ehr_timelinerefresh", "refreshed_at", "UPDATE"),
    ("ehr_timelineprojection", "effective_at", "UPDATE"),
    ("ehr_timelineprojection", "provenance", "UPDATE"),
    ("ehr_timelineprojection", "source_revision", "UPDATE"),
    ("ehr_timelineprojection", "encounter_id", "UPDATE"),
    ("ehr_timelineprojection", "episode_id", "UPDATE"),
}
RESULTS: Final = {
    (
        "ehr_patient_permission",
        "permission text, requested_clinic uuid, requested_patient uuid",
    ): "boolean",
    ("ehr_longitudinal_guard", ""): "trigger",
    ("ehr_note_visible", "requested_document uuid"): "boolean",
    ("ehr_note_author", "requested_document uuid"): "boolean",
    ("ehr_note_grantee", "requested_clinic uuid, requested_user uuid"): "boolean",
    ("ehr_note_guard", ""): "trigger",
    ("ehr_copy_finalization_guard", ""): "trigger",
    (
        "ehr_timeline_visible",
        "source_kind text, source uuid, requested_clinic uuid, requested_patient uuid",
    ): "boolean",
    ("ehr_timeline_enqueue", ""): "trigger",
    ("ehr_timeline_due", ""): "SETOF uuid",
    ("ehr_timeline_refresh", "requested_refresh uuid"): "boolean",
}
POSTURE: Final = {
    "ehr_patient_permission": ("v", True, ["clinic_app", "clinic_resolver"]),
    "ehr_longitudinal_guard": ("v", False, ["clinic_resolver"]),
    "ehr_note_visible": ("v", True, ["clinic_app", "clinic_resolver"]),
    "ehr_note_author": ("v", True, ["clinic_app", "clinic_resolver"]),
    "ehr_note_grantee": ("v", True, ["clinic_app", "clinic_resolver"]),
    "ehr_note_guard": ("v", False, ["clinic_resolver"]),
    "ehr_copy_finalization_guard": ("v", False, ["clinic_resolver"]),
    "ehr_timeline_visible": ("v", True, ["clinic_app", "clinic_resolver"]),
    "ehr_timeline_enqueue": ("v", False, ["clinic_resolver"]),
    "ehr_timeline_due": ("v", True, ["clinic_app", "clinic_resolver"]),
    "ehr_timeline_refresh": ("v", True, ["clinic_app", "clinic_resolver"]),
}
