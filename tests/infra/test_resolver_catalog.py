from typing import Final

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)

RESOLVER_TABLE_GRANTS: Final = {
    ("billing_invoice", "SELECT"),
    ("billing_invoicerevision", "INSERT"),
    ("billing_invoicerevision", "SELECT"),
    ("billing_paymentevent", "SELECT"),
    ("billing_pixcharge", "SELECT"),
    ("billing_pixoperation", "SELECT"),
    ("billing_receipt", "INSERT"),
    ("billing_receipt", "SELECT"),
    ("billing_settlement", "SELECT"),
    ("comms_appointmentreminder", "INSERT"),
    ("comms_appointmentreminder", "SELECT"),
    ("comms_integrationoperation", "INSERT"),
    ("comms_integrationoperation", "SELECT"),
    ("consent_consentacceptance", "SELECT"),
    ("consent_consentrevocation", "SELECT"),
    ("consent_consenttext", "SELECT"),
    ("ehr_allergy", "SELECT"),
    ("ehr_clinicalattachment", "SELECT"),
    ("ehr_clinicaldocument", "SELECT"),
    ("ehr_clinicaldocumentversion", "SELECT"),
    ("ehr_discarded_content", "INSERT"),
    ("ehr_encounter", "SELECT"),
    ("ehr_encounterintakereference", "SELECT"),
    ("ehr_historyassessment", "SELECT"),
    ("ehr_problem", "SELECT"),
    ("ehr_specialtytemplate", "SELECT"),
    ("identity_clinic", "SELECT"),
    ("identity_clinicconfiguration", "SELECT"),
    ("identity_organization", "SELECT"),
    ("identity_user", "SELECT"),
    ("identity_userclinicrole", "SELECT"),
    ("intake_patient", "SELECT"),
    ("intake_patientaccessgrant", "SELECT"),
    ("intake_patientchannelpreference", "SELECT"),
    ("intake_patientclinicenrollment", "SELECT"),
    ("intake_patientcontact", "SELECT"),
    ("intake_patientsession", "INSERT"),
    ("intake_patientsession", "SELECT"),
    ("intake_questionnaireevent", "INSERT"),
    ("intake_questionnaireevent", "SELECT"),
    ("intake_questionnaireresponse", "SELECT"),
    ("intake_questionnairetemplate", "SELECT"),
    ("prescription_prescriptiondocument", "SELECT"),
    ("prescription_prescriptiondocumentrelease", "SELECT"),
    ("prescription_prescriptiondocumentrevocation", "SELECT"),
    ("prescription_prescriptiondraft", "SELECT"),
    ("prescription_signaturecallback", "SELECT"),
    ("prescription_signatureoperation", "SELECT"),
    ("prescription_verificationprobe", "DELETE"),
    ("prescription_verificationprobe", "INSERT"),
    ("prescription_verificationprobe", "SELECT"),
    ("prescription_verificationprobe", "UPDATE"),
    ("retention_legalhold", "SELECT"),
    ("retention_recordexport", "SELECT"),
    ("retention_recordrelease", "SELECT"),
    ("retention_retentionpolicy", "SELECT"),
    ("scheduling_appointment", "SELECT"),
    ("scheduling_availabilityblock", "SELECT"),
    ("scheduling_patientbookingevent", "INSERT"),
    ("scheduling_waitlistentry", "SELECT"),
    ("scheduling_waitlistoffer", "SELECT"),
    ("teleconsult_teleconsultcredential", "SELECT"),
    ("teleconsult_teleconsultevent", "INSERT"),
    ("teleconsult_teleconsultevent", "SELECT"),
    ("teleconsult_teleconsultroom", "SELECT"),
    ("teleconsult_teleconsultsession", "SELECT"),
}
RESOLVER_COLUMN_GRANTS: Final = {
    ("billing_invoice", "state", "UPDATE"),
    ("comms_integrationoperation", "last_error", "UPDATE"),
    ("comms_integrationoperation", "status", "UPDATE"),
    ("comms_integrationoperation", "updated_at", "UPDATE"),
    ("identity_user", "id", "SELECT"),
    ("identity_user", "username", "SELECT"),
    ("intake_patientaccessgrant", "consumed_at", "UPDATE"),
    ("intake_patientsession", "idle_expires_at", "UPDATE"),
    ("intake_patientsession", "revoked_at", "UPDATE"),
    ("scheduling_availabilityblock", "id", "UPDATE"),
    ("teleconsult_teleconsultsession", "ended_at", "UPDATE"),
    ("teleconsult_teleconsultsession", "failure_reason", "UPDATE"),
    ("teleconsult_teleconsultsession", "revision", "UPDATE"),
    ("teleconsult_teleconsultsession", "started_at", "UPDATE"),
    ("teleconsult_teleconsultsession", "state", "UPDATE"),
}
POSTURE_OVERRIDES: Final = {
    "billing_immutable": ("v", False, ["clinic_resolver"]),
    "billing_invoice_guard": ("v", False, ["clinic_resolver"]),
    "billing_payment_event_guard": ("v", False, ["clinic_resolver"]),
    "billing_revision_snapshot": ("v", False, ["clinic_resolver"]),
    "billing_settlement_guard": ("v", False, ["clinic_resolver"]),
    "billing_settlement_receipt": ("v", False, ["clinic_resolver"]),
    "comms_schedule_reminders_v1": ("v", False, ["clinic_resolver"]),
    "configuration_guard": ("v", False, ["clinic_resolver"]),
    "consent_audit_scope": ("s", False, ["clinic_owner", "clinic_resolver"]),
    "consent_guard": ("v", False, ["clinic_resolver"]),
    "consent_immutable": ("v", False, ["clinic_resolver"]),
    "consent_session": ("s", True, ["clinic_app", "clinic_owner", "clinic_resolver"]),
    "ehr_attachment_guard": ("v", False, ["clinic_resolver"]),
    "ehr_binding_guard": ("v", False, ["clinic_resolver"]),
    "ehr_encounter_close_guard": ("v", False, ["clinic_resolver"]),
    "ehr_history_guard": ("v", False, ["clinic_resolver"]),
    "end_patient_session": ("v", True, ["clinic_app", "clinic_resolver"]),
    "overlay_plain_text_guard": ("v", False, ["clinic_resolver"]),
    "patient_booking_event_immutable": ("v", False, ["clinic_resolver"]),
    "patient_booking_guard": ("v", False, ["clinic_resolver"]),
    "patient_booking_lock_availability": (
        "v",
        True,
        ["clinic_app", "clinic_resolver"],
    ),
    "patient_booking_receipt": ("v", False, ["clinic_resolver"]),
    "prescription_close_guard": ("v", False, ["clinic_resolver"]),
    "prescription_document_guard": ("v", False, ["clinic_resolver"]),
    "prescription_document_open": ("s", False, ["clinic_resolver"]),
    "prescription_draft_guard": ("v", False, ["clinic_resolver"]),
    "prescription_item_guard": ("v", False, ["clinic_resolver"]),
    "prescription_patient_document_bytes": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "prescription_patient_documents": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "prescription_release_guard": ("v", False, ["clinic_resolver"]),
    "prescription_revocation_guard": ("v", False, ["clinic_resolver"]),
    "prescription_signature_callback_guard": ("v", False, ["clinic_resolver"]),
    "prescription_signature_guard": ("v", False, ["clinic_resolver"]),
    "prescription_verify_allowance": ("v", True, ["clinic_app", "clinic_resolver"]),
    "prescription_verify_synthetic": ("s", False, ["clinic_resolver"]),
    "protected_tenant": ("s", False, ["clinic_owner", "clinic_resolver"]),
    "questionnaire_immutable": ("v", False, ["clinic_resolver"]),
    "questionnaire_receipt": ("v", False, ["clinic_resolver"]),
    "questionnaire_response_guard": ("v", False, ["clinic_resolver"]),
    "questionnaire_staff": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "redeem_patient_invitation": ("v", True, ["clinic_app", "clinic_resolver"]),
    "retention_guard": ("v", False, ["clinic_resolver"]),
    "retention_immutable": ("v", False, ["clinic_resolver"]),
    "retention_patient_releases": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "retention_records_session": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "teleconsult_assigned": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "teleconsult_binding_guard": ("v", False, ["clinic_resolver"]),
    "teleconsult_credential_guard": ("v", False, ["clinic_resolver"]),
    "teleconsult_fail": (
        "v",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "teleconsult_immutable": ("v", False, ["clinic_resolver"]),
    "teleconsult_patient_match": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "teleconsult_room_state": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "teleconsult_session_guard": ("v", False, ["clinic_resolver"]),
    "teleconsult_session_scope": (
        "s",
        True,
        ["clinic_app", "clinic_owner", "clinic_resolver"],
    ),
    "touch_patient_session": ("v", True, ["clinic_app", "clinic_resolver"]),
    "waitlist_binding": ("v", False, ["clinic_resolver"]),
}
FUNCTION_SIGNATURES: Final = {
    ("auth_lookup", "requested_username text"),
    ("billing_immutable", ""),
    ("billing_invoice_guard", ""),
    ("billing_patient_charge", "requested_invoice uuid"),
    ("billing_patient_charges", ""),
    ("billing_payment_event_guard", ""),
    ("billing_payment_event_recorder", "requested_operation uuid"),
    (
        "billing_payment_event_recovered",
        "requested_provider text, requested_operation uuid, "
        "requested_event_id text, observed_status text, observed_amount bigint, "
        "observed_currency character varying",
    ),
    (
        "billing_payment_event_scope",
        "requested_provider text, requested_reference text",
    ),
    ("billing_payment_event_seen", "requested_provider text, requested_event_id text"),
    (
        "billing_payment_event_superseded",
        "requested_operation uuid, observed_at timestamp with time zone, "
        "observed_status text, observed_amount bigint, "
        "observed_currency character varying",
    ),
    ("billing_revision_snapshot", ""),
    ("billing_settlement_guard", ""),
    ("billing_settlement_receipt", ""),
    ("billing_staff_invoice", "requested_invoice uuid"),
    ("comms_due_reminders_v1", ""),
    (
        "comms_operation_callback_scope",
        "requested_provider text, requested_reference text",
    ),
    ("comms_operation_scope", "requested_operation uuid"),
    ("comms_operation_state_counts_v1", ""),
    ("comms_recover_pending_v1", ""),
    ("comms_schedule_reminders_v1", ""),
    ("configuration_guard", ""),
    ("consent_audit_scope", "record_id uuid, event_name text"),
    ("consent_guard", ""),
    ("consent_immutable", ""),
    ("consent_session", ""),
    ("ehr_assigned", "requested_encounter uuid"),
    ("ehr_attachment_guard", ""),
    ("ehr_binding_guard", ""),
    ("ehr_care", "requested_encounter uuid"),
    ("ehr_encounter_close_guard", ""),
    ("ehr_history_care", "requested_encounter uuid"),
    ("ehr_history_guard", ""),
    ("ehr_next_version", "requested_document uuid"),
    ("ehr_version_scope", "requested_clinic uuid, requested_version uuid"),
    ("end_patient_session", "requested_session uuid"),
    ("identity_queue_quotas", "requested_org uuid"),
    ("list_active_clinic_physicians", "requested_clinic uuid"),
    ("load_current_user", ""),
    ("overlay_plain_text_guard", ""),
    ("patient_booking_event_immutable", ""),
    ("patient_booking_guard", ""),
    (
        "patient_booking_lock_availability",
        "practitioners uuid[], starts timestamp with time zone[], "
        "ends timestamp with time zone[]",
    ),
    ("patient_booking_practitioner", "requested_practitioner uuid"),
    ("patient_booking_receipt", ""),
    ("patient_booking_scope", ""),
    ("patient_booking_slots", "requested_day date, ignored_appointment uuid"),
    ("patient_session_overview", "kek text"),
    ("prescription_close_guard", ""),
    ("prescription_document_guard", ""),
    (
        "prescription_document_open",
        "kek text, organization_id uuid, purpose text, envelope bytea",
    ),
    ("prescription_draft_guard", ""),
    ("prescription_item_guard", ""),
    ("prescription_patient_document_bytes", "kek text, requested_document uuid"),
    ("prescription_patient_documents", ""),
    ("prescription_patient_documents", "kek text"),
    ("prescription_release_guard", ""),
    ("prescription_revocation_guard", ""),
    ("prescription_signature_callback_guard", ""),
    (
        "prescription_signature_callback_scope",
        "requested_provider text, requested_reference text",
    ),
    ("prescription_signature_guard", ""),
    ("prescription_signature_scope", "requested_operation uuid"),
    (
        "prescription_verify",
        "kek text, requested_handle text, allow_synthetic boolean",
    ),
    (
        "prescription_verify_allowance",
        "requested_probe bytea, max_lookups integer, window_seconds integer",
    ),
    (
        "prescription_verify_synthetic",
        "signed_bytes bytea, content_digest text, signer_subject text, "
        "operation_ref text, not_before timestamp with time zone",
    ),
    ("protected_tenant", ""),
    (
        "questionnaire_completion",
        "requested_clinic uuid, requested_enrollment uuid",
    ),
    ("questionnaire_immutable", ""),
    ("questionnaire_patient_enrollment", ""),
    ("questionnaire_receipt", ""),
    ("questionnaire_response_guard", ""),
    ("questionnaire_staff", "requested_clinic uuid, roles text[]"),
    ("redeem_patient_invitation", "requested_clinic uuid, requested_hash bytea"),
    ("retention_author_label", "requested_user uuid"),
    ("retention_care", "requested_clinic uuid, requested_patient uuid"),
    ("retention_care_patients", "requested_clinic uuid"),
    ("retention_guard", ""),
    ("retention_immutable", ""),
    ("retention_patient_releases", ""),
    ("retention_record_scope", "requested_class text, requested_record uuid"),
    ("retention_records_session", ""),
    ("teleconsult_assigned", "requested_session uuid"),
    ("teleconsult_binding_guard", ""),
    ("teleconsult_credential_guard", ""),
    ("teleconsult_fail", "requested_session uuid, reason text"),
    ("teleconsult_immutable", ""),
    ("teleconsult_patient_match", "requested_session uuid"),
    ("teleconsult_room_state", "requested_session uuid"),
    ("teleconsult_session_guard", ""),
    ("teleconsult_session_scope", "requested_session uuid"),
    ("touch_patient_session", "requested_session uuid"),
    ("user_has_org", "requested_org uuid"),
    ("user_organizations", ""),
    ("waitlist_binding", ""),
    ("waitlist_staff", "requested_clinic uuid"),
}
FUNCTION_RESULTS: Final = {
    ("auth_lookup", "requested_username text"): (
        "TABLE(id uuid, username character varying, "
        "password character varying, is_active boolean)"
    ),
    ("billing_immutable", ""): "trigger",
    ("billing_invoice_guard", ""): "trigger",
    ("billing_patient_charge", "requested_invoice uuid"): (
        "TABLE(invoice_id uuid, reference uuid, amount_minor bigint, "
        "currency character varying, state character varying, "
        "issued_at timestamp with time zone, receipt_reference uuid, "
        "receipt_issued_at timestamp with time zone, copy_code bytea, "
        "qr_base64 bytea, expires_at timestamp with time zone, "
        "flagged boolean, clinic_timezone character varying)"
    ),
    ("billing_patient_charges", ""): (
        "TABLE(invoice_id uuid, reference uuid, amount_minor bigint, "
        "currency character varying, state character varying, "
        "issued_at timestamp with time zone, receipt_reference uuid, "
        "receipt_issued_at timestamp with time zone)"
    ),
    ("billing_payment_event_guard", ""): "trigger",
    ("billing_payment_event_recorder", "requested_operation uuid"): "boolean",
    (
        "billing_payment_event_recovered",
        "requested_provider text, requested_operation uuid, "
        "requested_event_id text, observed_status text, observed_amount bigint, "
        "observed_currency character varying",
    ): "boolean",
    (
        "billing_payment_event_scope",
        "requested_provider text, requested_reference text",
    ): (
        "TABLE(operation_id uuid, invoice_id uuid, organization_id uuid, "
        "clinic_id uuid, actor_id uuid, invoice_state character varying, "
        "invoice_amount_minor bigint, invoice_currency character varying, "
        "operation_amount_minor bigint, operation_currency character varying, "
        "invoice_reference uuid, expires_at timestamp with time zone, "
        "settled_amount_minor bigint, settled_currency character varying)"
    ),
    (
        "billing_payment_event_seen",
        "requested_provider text, requested_event_id text",
    ): "boolean",
    (
        "billing_payment_event_superseded",
        "requested_operation uuid, observed_at timestamp with time zone, "
        "observed_status text, observed_amount bigint, "
        "observed_currency character varying",
    ): "boolean",
    ("billing_revision_snapshot", ""): "trigger",
    ("billing_settlement_guard", ""): "trigger",
    ("billing_settlement_receipt", ""): "trigger",
    ("billing_staff_invoice", "requested_invoice uuid"): "boolean",
    ("comms_due_reminders_v1", ""): "SETOF uuid",
    (
        "comms_operation_callback_scope",
        "requested_provider text, requested_reference text",
    ): "TABLE(operation_id uuid, organization_id uuid, clinic_id uuid, actor_id uuid)",
    ("comms_operation_scope", "requested_operation uuid"): (
        "TABLE(organization_id uuid, clinic_id uuid, actor_id uuid)"
    ),
    ("comms_operation_state_counts_v1", ""): (
        "TABLE(status text, operation_count bigint, "
        "oldest_created_at timestamp with time zone)"
    ),
    ("comms_recover_pending_v1", ""): "SETOF uuid",
    ("comms_schedule_reminders_v1", ""): "trigger",
    ("configuration_guard", ""): "trigger",
    ("consent_audit_scope", "record_id uuid, event_name text"): (
        "TABLE(session_id uuid, organization_id uuid, clinic_id uuid)"
    ),
    ("consent_guard", ""): "trigger",
    ("consent_immutable", ""): "trigger",
    ("consent_session", ""): (
        "TABLE(session_id uuid, organization_id uuid, clinic_id uuid, "
        "patient_id uuid, enrollment_id uuid)"
    ),
    ("ehr_assigned", "requested_encounter uuid"): "boolean",
    ("ehr_attachment_guard", ""): "trigger",
    ("ehr_binding_guard", ""): "trigger",
    ("ehr_care", "requested_encounter uuid"): "boolean",
    ("ehr_encounter_close_guard", ""): "trigger",
    ("ehr_history_care", "requested_encounter uuid"): "boolean",
    ("ehr_history_guard", ""): "trigger",
    ("ehr_next_version", "requested_document uuid"): "integer",
    ("ehr_version_scope", "requested_clinic uuid, requested_version uuid"): "uuid",
    ("end_patient_session", "requested_session uuid"): "void",
    ("identity_queue_quotas", "requested_org uuid"): "jsonb",
    ("list_active_clinic_physicians", "requested_clinic uuid"): (
        "TABLE(user_id uuid, display_label text)"
    ),
    ("load_current_user", ""): "SETOF identity_user",
    ("overlay_plain_text_guard", ""): "trigger",
    ("patient_booking_event_immutable", ""): "trigger",
    ("patient_booking_guard", ""): "trigger",
    (
        "patient_booking_lock_availability",
        "practitioners uuid[], starts timestamp with time zone[], "
        "ends timestamp with time zone[]",
    ): "SETOF uuid",
    ("patient_booking_practitioner", "requested_practitioner uuid"): "boolean",
    ("patient_booking_receipt", ""): "trigger",
    ("patient_booking_scope", ""): (
        "TABLE(session_id uuid, organization_id uuid, clinic_id uuid, "
        "patient_id uuid, enrollment_id uuid, clinic_name character varying, "
        "timezone character varying)"
    ),
    ("patient_booking_slots", "requested_day date, ignored_appointment uuid"): (
        "TABLE(practitioner_id uuid, display_label text, "
        "start_at timestamp with time zone, end_at timestamp with time zone)"
    ),
    ("patient_session_overview", "kek text"): (
        "TABLE(session_id uuid, patient_name character varying, "
        "clinic_name character varying, enrolled_at timestamp with time zone, "
        "operations text[], expires_at timestamp with time zone, "
        "idle_expires_at timestamp with time zone)"
    ),
    ("prescription_close_guard", ""): "trigger",
    ("prescription_document_guard", ""): "trigger",
    (
        "prescription_document_open",
        "kek text, organization_id uuid, purpose text, envelope bytea",
    ): "bytea",
    ("prescription_draft_guard", ""): "trigger",
    ("prescription_item_guard", ""): "trigger",
    ("prescription_patient_document_bytes", "kek text, requested_document uuid"): (
        "bytea"
    ),
    ("prescription_patient_documents", ""): (
        "TABLE(document_id uuid, document_version integer, state text, "
        "issued_at timestamp with time zone)"
    ),
    ("prescription_patient_documents", "kek text"): (
        "TABLE(document_id uuid, document_version integer, state text, "
        "issued_at timestamp with time zone, verification_url text)"
    ),
    ("prescription_release_guard", ""): "trigger",
    ("prescription_revocation_guard", ""): "trigger",
    ("prescription_signature_callback_guard", ""): "trigger",
    (
        "prescription_signature_callback_scope",
        "requested_provider text, requested_reference text",
    ): (
        "TABLE(operation_id uuid, organization_id uuid, clinic_id uuid, "
        "actor_id uuid, provider text)"
    ),
    ("prescription_signature_guard", ""): "trigger",
    ("prescription_signature_scope", "requested_operation uuid"): (
        "TABLE(organization_id uuid, clinic_id uuid, actor_id uuid, provider text)"
    ),
    (
        "prescription_verify",
        "kek text, requested_handle text, allow_synthetic boolean",
    ): (
        "TABLE(status text, document_version integer, "
        "issued_at timestamp with time zone, content_digest text, "
        "signed_digest text, issuer_label text, clinic_label text)"
    ),
    (
        "prescription_verify_allowance",
        "requested_probe bytea, max_lookups integer, window_seconds integer",
    ): "boolean",
    (
        "prescription_verify_synthetic",
        "signed_bytes bytea, content_digest text, signer_subject text, "
        "operation_ref text, not_before timestamp with time zone",
    ): "boolean",
    ("protected_tenant", ""): "uuid",
    (
        "questionnaire_completion",
        "requested_clinic uuid, requested_enrollment uuid",
    ): "TABLE(response_id uuid, state character varying, revision integer)",
    ("questionnaire_immutable", ""): "trigger",
    ("questionnaire_patient_enrollment", ""): "uuid",
    ("questionnaire_receipt", ""): "trigger",
    ("questionnaire_response_guard", ""): "trigger",
    ("questionnaire_staff", "requested_clinic uuid, roles text[]"): "boolean",
    ("redeem_patient_invitation", "requested_clinic uuid, requested_hash bytea"): (
        "TABLE(session_id uuid)"
    ),
    ("retention_author_label", "requested_user uuid"): "text",
    ("retention_care", "requested_clinic uuid, requested_patient uuid"): "boolean",
    ("retention_care_patients", "requested_clinic uuid"): "TABLE(patient_id uuid)",
    ("retention_guard", ""): "trigger",
    ("retention_immutable", ""): "trigger",
    ("retention_patient_releases", ""): (
        "TABLE(release_id uuid, version_id uuid, document_id uuid, "
        "encounter_id uuid, version_number integer, state text, "
        "content_digest text, finalized_at timestamp with time zone, "
        "amendment_of_version integer, author_label text, content bytea, "
        "released_at timestamp with time zone)"
    ),
    ("retention_record_scope", "requested_class text, requested_record uuid"): (
        "TABLE(record_clinic uuid, record_organization uuid, "
        "record_created timestamp with time zone)"
    ),
    ("retention_records_session", ""): (
        "TABLE(session_id uuid, organization_id uuid, clinic_id uuid, patient_id uuid)"
    ),
    ("teleconsult_assigned", "requested_session uuid"): "boolean",
    ("teleconsult_binding_guard", ""): "trigger",
    ("teleconsult_credential_guard", ""): "trigger",
    ("teleconsult_fail", "requested_session uuid, reason text"): "boolean",
    ("teleconsult_immutable", ""): "trigger",
    ("teleconsult_patient_match", "requested_session uuid"): "boolean",
    ("teleconsult_room_state", "requested_session uuid"): "text",
    ("teleconsult_session_guard", ""): "trigger",
    ("teleconsult_session_scope", "requested_session uuid"): (
        "TABLE(clinic_id uuid, patient_id uuid, physician_id uuid, "
        "state character varying, encounter_state character varying, "
        "consent_id uuid)"
    ),
    ("touch_patient_session", "requested_session uuid"): (
        "TABLE(session_id uuid, organization_id uuid, clinic_id uuid, "
        "patient_id uuid, enrollment_id uuid, operations text[], "
        "expires_at timestamp with time zone, "
        "idle_expires_at timestamp with time zone)"
    ),
    ("user_has_org", "requested_org uuid"): "boolean",
    ("user_organizations", ""): "SETOF uuid",
    ("waitlist_binding", ""): "trigger",
    ("waitlist_staff", "requested_clinic uuid"): "boolean",
}


def test_resolver_table_privileges_are_an_exact_allowlist() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_resolver' AND table_schema = 'clinic_app'
            UNION ALL
            SELECT '<default-table>', acl.privilege_type
            FROM pg_catalog.pg_default_acl AS defaults
            CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
            JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE defaults.defaclobjtype = 'r'
              AND grantee.rolname = 'clinic_resolver'
            """
        )
        table_grants = set(cursor.fetchall())
        cursor.execute(
            """
            SELECT class.relname, attribute.attname, acl.privilege_type
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_class AS class ON class.oid = attribute.attrelid
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS acl
            JOIN pg_catalog.pg_roles AS grantee ON grantee.oid = acl.grantee
            WHERE namespace.nspname = 'clinic_app'
              AND grantee.rolname = 'clinic_resolver'
            """
        )
        column_grants = cursor.fetchall()

    assert table_grants == RESOLVER_TABLE_GRANTS
    assert set(column_grants) == RESOLVER_COLUMN_GRANTS


def test_resolver_functions_have_exact_hardened_catalog_posture() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT procedure.proname,
                   pg_catalog.pg_get_function_identity_arguments(procedure.oid),
                   procedure.proowner::regrole::text,
                   procedure.prosecdef, procedure.provolatile,
                   procedure.proparallel, procedure.proconfig,
                   NOT EXISTS (
                       SELECT 1
                       FROM pg_catalog.aclexplode(COALESCE(
                           procedure.proacl,
                           pg_catalog.acldefault('f', procedure.proowner)
                       )) AS acl
                       WHERE acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                   ),
                   pg_catalog.has_function_privilege(
                       'clinic_app', procedure.oid, 'EXECUTE'
                   ),
                   ARRAY(
                       SELECT COALESCE(grantee.rolname, 'PUBLIC')
                       FROM pg_catalog.aclexplode(COALESCE(
                           procedure.proacl,
                           pg_catalog.acldefault('f', procedure.proowner)
                       )) AS acl
                       LEFT JOIN pg_catalog.pg_roles AS grantee
                         ON grantee.oid = acl.grantee
                       WHERE acl.privilege_type = 'EXECUTE'
                       ORDER BY 1
                   ),
                   procedure.prorows,
                   pg_catalog.pg_get_function_result(procedure.oid),
                   pg_catalog.pg_get_functiondef(procedure.oid)
            FROM pg_catalog.pg_proc AS procedure
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = procedure.pronamespace
            WHERE namespace.nspname = 'clinic_app'
              AND procedure.proowner = 'clinic_resolver'::pg_catalog.regrole
            ORDER BY procedure.proname
            """
        )
        functions = cursor.fetchall()

    assert {(row[0], row[1]) for row in functions} == FUNCTION_SIGNATURES
    assert len(functions) == len(FUNCTION_SIGNATURES)
    for function in functions:
        name = function[0]
        volatility, app_execute, executors = POSTURE_OVERRIDES.get(
            name, ("s", True, ["clinic_app", "clinic_resolver"])
        )
        assert function[2:9] == (
            "clinic_resolver",
            True,
            volatility,
            "u",
            ["search_path=pg_catalog, clinic_app, pg_temp"],
            True,
            app_execute,
        )
        assert function[9] == executors
        if name == "load_current_user":
            assert function[10] == 1.0
        assert "EXECUTE " not in function[12].upper()

    results = {(row[0], row[1]): row[11] for row in functions}
    assert results == FUNCTION_RESULTS
