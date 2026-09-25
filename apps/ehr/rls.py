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
}
