"""Define the closed logical-recovery archive and source-scope contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Never

DOMAIN_RELATIONS: Final = (
    "audit_event",
    "billing_invoice",
    "billing_invoicerevision",
    "billing_paymentevent",
    "billing_pixcharge",
    "billing_pixoperation",
    "billing_receipt",
    "billing_settlement",
    "comms_appointmentreminder",
    "comms_integrationoperation",
    "consent_consentacceptance",
    "consent_consentrevocation",
    "consent_consenttext",
    "ehr_allergy",
    "ehr_clinicalattachment",
    "ehr_clinicaldocument",
    "ehr_clinicaldocumentversion",
    "ehr_discarded_content",
    "ehr_encounter",
    "ehr_encounterintakereference",
    "ehr_historyassessment",
    "ehr_problem",
    "ehr_specialtytemplate",
    "identity_clinic",
    "identity_clinicconfiguration",
    "identity_organization",
    "identity_physicianevidence",
    "identity_physicianprofile",
    "identity_user",
    "identity_userclinicrole",
    "intake_patient",
    "intake_patientaccessgrant",
    "intake_patientchannelpreference",
    "intake_patientclinicenrollment",
    "intake_patientcontact",
    "intake_patientcontactevent",
    "intake_patientsession",
    "intake_questionnaireevent",
    "intake_questionnaireresponse",
    "intake_questionnairetemplate",
    "otp_totp_totpdevice",
    "prescription_prescriptiondocument",
    "prescription_prescriptiondocumentrelease",
    "prescription_prescriptiondocumentrevocation",
    "prescription_prescriptiondraft",
    "prescription_prescriptionitem",
    "prescription_signaturecallback",
    "prescription_signatureoperation",
    "prescription_verificationprobe",
    "retention_legalhold",
    "retention_recordexport",
    "retention_recordrelease",
    "retention_retentionpolicy",
    "scheduling_appointment",
    "scheduling_availabilityblock",
    "scheduling_patientbookingevent",
    "scheduling_waitlistentry",
    "scheduling_waitlistoffer",
    "teleconsult_teleconsultcredential",
    "teleconsult_teleconsultevent",
    "teleconsult_teleconsultroom",
    "teleconsult_teleconsultsession",
    "tenancy_tenantdatakey",
)
TABLE_DATA: Final = tuple(f"clinic_app.{relation}" for relation in DOMAIN_RELATIONS)
SEQUENCE_TARGETS: Final = {
    "audit_event_seq_seq": ("audit_event", "seq"),
    "otp_totp_totpdevice_id_seq": ("otp_totp_totpdevice", "id"),
    "scheduling_waitlistentry_id_seq": ("scheduling_waitlistentry", "id"),
    "teleconsult_teleconsultevent_id_seq": ("teleconsult_teleconsultevent", "id"),
}
SEQUENCE_SET: Final = tuple(f"clinic_app.{name}" for name in sorted(SEQUENCE_TARGETS))
REQUIRED_EMPTY: Final = (
    "identity_user_groups",
    "identity_user_user_permissions",
    "otp_static_staticdevice",
    "otp_static_statictoken",
    "tenancy_tenantprobe",
)
EXCLUDED_PREFIXES: Final = ("auth_",)
EXCLUDED_RELATIONS: Final = (
    "django_admin_log",
    "django_content_type",
    "django_migrations",
    "django_session",
    *REQUIRED_EMPTY,
)
POSTGRES_VERSION: Final = "16.14"
POSTGRES_IDENTIFIER_MAX_LENGTH: Final = 63


class RestoreContractError(RuntimeError):
    """Reject recovery input before any target mutation."""


@dataclass(frozen=True, slots=True)
class SourceScope:
    """Carry only aggregate pre-dump observations, never restored data."""

    active_writer_count: int
    empty_relation_counts: dict[str, int]
    foreign_audit_organization_count: int
    identity_user_count: int
    organization_count: int
    tenant_data_key_count: int
    totp_without_identity_count: int
    unexpected_domain_relations: tuple[str, ...]
    users_without_role_count: int


def dump_argv(database: str) -> tuple[str, ...]:
    """Build the exact container-resident custom archive command."""
    _database_name(database)
    selectors = tuple(
        item
        for relation in (*TABLE_DATA, *SEQUENCE_SET)
        for item in ("--table", relation)
    )
    return (
        "pg_dump",
        "--data-only",
        "--format=custom",
        "--column-inserts",
        "--strict-names",
        *selectors,
        "--host=127.0.0.1",
        "--username=clinic_super",
        database,
    )


def restore_argv(database: str) -> tuple[str, ...]:
    """Build the exact target-container strict data-only restore command."""
    _database_name(database)
    selectors = tuple(
        f"--table={relation.removeprefix('clinic_app.')}"
        for relation in (*TABLE_DATA, *SEQUENCE_SET)
    )
    return (
        "pg_restore",
        "--schema=clinic_app",
        "--strict-names",
        "--data-only",
        "--no-owner",
        "--no-acl",
        "--single-transaction",
        "--exit-on-error",
        # Guards and binding triggers enforce write-time invariants; during a
        # data-only restore the rows are already valid and load order is not
        # dependency order, so the superuser session disables them. Posture,
        # fingerprint, chain and RLS proofs run after the load completes.
        "--disable-triggers",
        f"--dbname={database}",
        "--host=127.0.0.1",
        "--username=clinic_super",
        *selectors,
    )


def normalize_toc(raw: str) -> tuple[str, ...]:
    """Normalize only TABLE DATA and SEQUENCE SET archive-list records."""
    records: list[str] = []
    for line in raw.splitlines():
        if not line or line.startswith(";"):
            continue
        _, separator, payload = line.partition(";")
        if not separator:
            _fail("archive table of contents is malformed")
        fields = payload.split()
        for kind in (("TABLE", "DATA"), ("SEQUENCE", "SET")):
            try:
                index = next(
                    position
                    for position in range(len(fields) - 1)
                    if tuple(fields[position : position + 2]) == kind
                )
            except StopIteration:
                continue
            if len(fields) <= index + 3:
                _fail("archive table of contents is malformed")
            records.append(f"{' '.join(kind)} {fields[index + 2]}.{fields[index + 3]}")
            break
    if len(records) != len(set(records)):
        _fail("archive table of contents contains duplicate manifest entries")
    return tuple(sorted(records))


def expected_toc() -> tuple[str, ...]:
    """Return the byte-order canonical fixed archive manifest."""
    return tuple(
        sorted(
            [
                *(f"TABLE DATA {relation}" for relation in TABLE_DATA),
                *(f"SEQUENCE SET {relation}" for relation in SEQUENCE_SET),
            ]
        )
    )


def require_exact_toc(raw: str) -> None:
    """Reject missing, extra, reordered-normalization, or wrong-scope entries."""
    if normalize_toc(raw) != expected_toc():
        _fail("archive manifest does not equal the fixed recovery manifest")


def require_source_scope(scope: SourceScope) -> None:
    """Reject any source that is not the exact stopped synthetic tenant."""
    if scope.active_writer_count != 0:
        _fail("source has an active writer")
    if scope.organization_count != 1:
        _fail("source must contain exactly one synthetic organization")
    if scope.identity_user_count < 1 or scope.users_without_role_count != 0:
        _fail("every source identity requires one organization role")
    if scope.totp_without_identity_count != 0:
        _fail("source TOTP scope is not identity-bound")
    if scope.foreign_audit_organization_count != 0:
        _fail("source audit organization scope is not closed")
    if scope.tenant_data_key_count < 1:
        _fail("source has no tenant data key to prove restorable")
    if set(scope.empty_relation_counts) != set(REQUIRED_EMPTY) or any(
        count != 0 for count in scope.empty_relation_counts.values()
    ):
        _fail("required-empty source relations are not empty")
    if scope.unexpected_domain_relations:
        _fail("source contains an unexpected domain relation")


def _database_name(value: str) -> None:
    if (
        not value
        or len(value) > POSTGRES_IDENTIFIER_MAX_LENGTH
        or not value.replace("_", "a").isalnum()
    ):
        _fail("database name is invalid")


def _fail(message: str) -> Never:
    raise RestoreContractError(message)
