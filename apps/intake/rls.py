"""Versioned organization-RLS helpers owned by the intake domain."""

from typing import Final

INTAKE_RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("intake_patient", "organization_id"),
        ("intake_patientclinicenrollment", "organization_id"),
        ("intake_patientcontact", "organization_id"),
        ("intake_patientchannelpreference", "organization_id"),
        ("intake_patientcontactevent", "organization_id"),
        ("intake_patientaccessgrant", "organization_id"),
        ("intake_patientsession", "organization_id"),
        ("intake_patientdemographics", "organization_id"),
        ("intake_demographicscorrection", "organization_id"),
        ("intake_patientidentifier", "organization_id"),
        ("intake_patientaddress", "organization_id"),
        ("intake_emergencycontact", "organization_id"),
        ("intake_insurancemembership", "organization_id"),
        ("intake_clinicintakepolicy", "organization_id"),
        ("intake_duplicatecandidate", "organization_id"),
        ("intake_patientduplicateindex", "organization_id"),
        ("intake_mergedecision", "organization_id"),
        ("intake_patientalias", "organization_id"),
        ("intake_patientmergetransfer", "organization_id"),
    }
)

# Posture registry declarations consumed by apps.tenancy.posture.
RLS_TARGETS: Final[frozenset[tuple[str, str]]] = INTAKE_RLS_TARGETS
# Questionnaire tables carry bespoke staff/patient policies declared in
# apps/intake/migrations/_questionnaire_sql.py.
CUSTOM_RLS_TABLES: Final[frozenset[str]] = frozenset(
    {
        "intake_questionnairetemplate",
        "intake_questionnaireresponse",
        "intake_questionnaireevent",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final[dict[str, frozenset[str]]] = {
    "intake_patient": frozenset({"SELECT", "INSERT"}),
    "intake_patientclinicenrollment": frozenset({"SELECT", "INSERT"}),
    "intake_patientcontact": frozenset({"SELECT", "INSERT"}),
    "intake_patientchannelpreference": frozenset({"SELECT", "INSERT"}),
    "intake_patientcontactevent": frozenset({"SELECT", "INSERT"}),
    "intake_patientaccessgrant": frozenset({"SELECT", "INSERT"}),
    # Sessions are minted only by the resolver-owned redemption function.
    "intake_patientsession": frozenset({"SELECT"}),
    "intake_questionnairetemplate": frozenset({"SELECT", "INSERT"}),
    "intake_questionnaireresponse": frozenset({"SELECT", "INSERT"}),
    "intake_questionnaireevent": frozenset({"SELECT"}),
    "intake_patientdemographics": frozenset({"SELECT", "INSERT"}),
    "intake_demographicscorrection": frozenset({"SELECT", "INSERT"}),
    "intake_patientidentifier": frozenset({"SELECT", "INSERT"}),
    "intake_patientaddress": frozenset({"SELECT", "INSERT"}),
    "intake_emergencycontact": frozenset({"SELECT", "INSERT"}),
    "intake_insurancemembership": frozenset({"SELECT", "INSERT"}),
    "intake_clinicintakepolicy": frozenset({"SELECT", "INSERT"}),
    "intake_duplicatecandidate": frozenset({"SELECT", "INSERT"}),
    "intake_patientduplicateindex": frozenset({"SELECT", "INSERT"}),
    "intake_mergedecision": frozenset({"SELECT", "INSERT"}),
    "intake_patientalias": frozenset({"SELECT", "INSERT"}),
    "intake_patientmergetransfer": frozenset({"SELECT", "INSERT"}),
}
# Column-level privileges held by the runtime role (pg_attribute.attacl).
COLUMN_GRANTS: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {
        ("intake_duplicatecandidate", "state", "UPDATE"),
        ("intake_patientalias", "active", "UPDATE"),
        ("intake_patientalias", "deactivated_at", "UPDATE"),
        ("intake_patientalias", "deactivated_by_id", "UPDATE"),
        ("intake_patientalias", "deactivation_session_id", "UPDATE"),
        ("intake_patientalias", "deactivation_reason", "UPDATE"),
        ("intake_patient", "full_name", "UPDATE"),
        ("intake_patient", "full_name_index", "UPDATE"),
        ("intake_patient", "birth_date", "UPDATE"),
        ("intake_patientaccessgrant", "revoked_at", "UPDATE"),
        ("intake_patientchannelpreference", "opted_in", "UPDATE"),
        ("intake_patientchannelpreference", "updated_at", "UPDATE"),
        ("intake_patientchannelpreference", "version", "UPDATE"),
        ("intake_patientcontact", "destination", "UPDATE"),
        ("intake_patientcontact", "destination_version", "UPDATE"),
        ("intake_patientcontact", "updated_at", "UPDATE"),
        ("intake_patientcontact", "verification_method", "UPDATE"),
        ("intake_patientcontact", "verified_at", "UPDATE"),
        ("intake_patientcontact", "verified_version", "UPDATE"),
        ("intake_patientsession", "revoked_at", "UPDATE"),
        ("intake_questionnaireresponse", "answers", "UPDATE"),
        ("intake_questionnaireresponse", "reopen_reason", "UPDATE"),
        ("intake_questionnaireresponse", "revision", "UPDATE"),
        ("intake_questionnaireresponse", "state", "UPDATE"),
        ("intake_questionnaireresponse", "submitted_at", "UPDATE"),
        ("intake_questionnaireresponse", "updated_at", "UPDATE"),
    }
)


def apply_intake_rls(table: str, tenant_column: str) -> str:
    """Build exact FORCE-RLS DDL for one versioned intake table."""
    if (table, tenant_column) not in INTAKE_RLS_TARGETS:
        raise KeyError((table, tenant_column))
    condition = (
        f"{tenant_column} = "
        "NULLIF(pg_catalog.current_setting('app.current_tenant', true), '')"
        "::pg_catalog.uuid"
    )
    return f"""
ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinic_app.{table}
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING ({condition})
    WITH CHECK ({condition});
"""


def remove_intake_rls(table: str, tenant_column: str) -> str:
    """Build exact reverse DDL for one versioned intake table."""
    if (table, tenant_column) not in INTAKE_RLS_TARGETS:
        raise KeyError((table, tenant_column))
    return f"""
DROP POLICY IF EXISTS tenant_isolation ON clinic_app.{table};
ALTER TABLE clinic_app.{table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} DISABLE ROW LEVEL SECURITY;
"""
