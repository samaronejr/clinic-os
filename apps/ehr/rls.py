"""Tenant posture declarations for the EHR domain.

Every EHR table carries bespoke clinical policies (``clinical_*``,
``attachment_*``, ``history_*``) declared in
``apps/ehr/migrations/_clinical_sql.py``, ``_attachment_sql.py`` and
``_history_sql.py``; none uses the shared ``tenant_isolation`` policy.
"""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "ehr_specialtytemplate",
        "ehr_encounter",
        "ehr_clinicaldocument",
        "ehr_clinicaldocumentversion",
        "ehr_encounterintakereference",
        "ehr_clinicalattachment",
        "ehr_historyassessment",
        "ehr_problem",
        "ehr_allergy",
        # Plan item 27: episodes and draft autosave state (_autosave_sql.py).
        "ehr_episode",
        "ehr_episodeencounter",
        "ehr_drafteditstate",
        "ehr_draftsavereceipt",
        "ehr_encounteraddendum",
        "ehr_addendumsavereceipt",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "ehr_specialtytemplate": frozenset({"SELECT", "INSERT"}),
    "ehr_encounter": frozenset({"SELECT", "INSERT"}),
    "ehr_clinicaldocument": frozenset({"SELECT", "INSERT"}),
    "ehr_clinicaldocumentversion": frozenset({"SELECT", "INSERT"}),
    "ehr_encounterintakereference": frozenset({"SELECT", "INSERT"}),
    "ehr_clinicalattachment": frozenset({"SELECT", "INSERT"}),
    "ehr_historyassessment": frozenset({"SELECT", "INSERT"}),
    "ehr_problem": frozenset({"SELECT", "INSERT"}),
    "ehr_allergy": frozenset({"SELECT", "INSERT"}),
    "ehr_episode": frozenset({"SELECT", "INSERT"}),
    "ehr_episodeencounter": frozenset({"SELECT", "INSERT"}),
    "ehr_drafteditstate": frozenset({"SELECT", "INSERT"}),
    "ehr_draftsavereceipt": frozenset({"SELECT", "INSERT"}),
    "ehr_encounteraddendum": frozenset({"SELECT", "INSERT"}),
    "ehr_addendumsavereceipt": frozenset({"SELECT", "INSERT"}),
}
# Column-level privileges held by the runtime role (pg_attribute.attacl).
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {
        ("ehr_clinicalattachment", "scan_attempts", "UPDATE"),
        ("ehr_clinicalattachment", "scan_reason", "UPDATE"),
        ("ehr_clinicalattachment", "scanned_at", "UPDATE"),
        ("ehr_clinicalattachment", "state", "UPDATE"),
        ("ehr_clinicaldocumentversion", "amendment_reason", "UPDATE"),
        ("ehr_clinicaldocumentversion", "content", "UPDATE"),
        ("ehr_clinicaldocumentversion", "content_digest", "UPDATE"),
        ("ehr_clinicaldocumentversion", "content_sha256", "UPDATE"),
        ("ehr_clinicaldocumentversion", "finalized_at", "UPDATE"),
        ("ehr_clinicaldocumentversion", "revision", "UPDATE"),
        ("ehr_clinicaldocumentversion", "state", "UPDATE"),
        ("ehr_clinicaldocumentversion", "updated_at", "UPDATE"),
        ("ehr_drafteditstate", "handover_requested_by", "UPDATE"),
        ("ehr_drafteditstate", "lock_expires_at", "UPDATE"),
        ("ehr_drafteditstate", "lock_holder", "UPDATE"),
        ("ehr_drafteditstate", "section_edit_epochs", "UPDATE"),
        ("ehr_drafteditstate", "updated_at", "UPDATE"),
        ("ehr_encounter", "closed_at", "UPDATE"),
        ("ehr_encounter", "revision", "UPDATE"),
        ("ehr_encounter", "state", "UPDATE"),
        ("ehr_encounteraddendum", "revision", "UPDATE"),
        ("ehr_encounteraddendum", "text", "UPDATE"),
        ("ehr_encounteraddendum", "text_sha256", "UPDATE"),
        ("ehr_encounteraddendum", "updated_at", "UPDATE"),
        ("ehr_episode", "closed_at", "UPDATE"),
        ("ehr_episode", "closed_by_id", "UPDATE"),
        ("ehr_episode", "state", "UPDATE"),
    }
)
