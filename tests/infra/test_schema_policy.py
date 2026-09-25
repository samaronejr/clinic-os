from typing import Final

import psycopg
import pytest
from apps.identity.models import (
    Clinic,
    ClinicConfiguration,
    Organization,
    UserClinicRole,
)
from apps.tenancy import posture
from apps.tenancy.models import TenantScopedModel
from django.apps import apps as django_apps
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)

# The expected tenant surface is derived from the per-app posture
# registries (apps/<app>/rls.py) aggregated by apps.tenancy.posture.
EXPECTED_TENANT_COLUMNS: Final = posture.expected_tenant_columns()
SELECT_ONLY_RUNTIME_TABLES: Final = posture.select_only_runtime_tables()
SELECT_INSERT_RUNTIME_TABLES: Final = posture.select_insert_runtime_tables()


def test_all_concrete_tenant_models_have_the_exact_rls_policy_set() -> None:
    # Given: every concrete TenantScopedModel plus the Organization tenant root
    tenant_models = {
        model._meta.db_table
        for model in django_apps.get_models()
        if issubclass(model, TenantScopedModel) and not model._meta.abstract
    }
    tenant_models.update(
        model._meta.db_table
        for model in (Organization, Clinic, ClinicConfiguration, UserClinicRole)
    )

    # When: table flags and the tenant-table policies are read
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT class.relname, class.relrowsecurity, class.relforcerowsecurity,
                   class.relowner::regrole::text
            FROM pg_catalog.pg_class AS class
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = class.relnamespace
            WHERE namespace.nspname = 'clinic_app'
              AND class.relname = ANY(%s)
            """,
            [list(EXPECTED_TENANT_COLUMNS)],
        )
        table_posture = set(cursor.fetchall())
        cursor.execute(
            """
            SELECT tablename, policyname
            FROM pg_catalog.pg_policies
            WHERE schemaname = 'clinic_app'
              AND tablename = ANY(%s)
            """,
            [list(EXPECTED_TENANT_COLUMNS)],
        )
        policies = set(cursor.fetchall())

    # Then: missing models, missing policies, and extra policies all fail
    # Task-16 session/clinical policies are checked independently, including
    # exact policy names, FORCE RLS and runtime ACLs, in test_questionnaires.
    assert tenant_models == set(EXPECTED_TENANT_COLUMNS) | {
        # Bounded append-only clinic settings: test_clinic_settings.
        "identity_clinicconfiguration",
        # Exact v1 scope policies: tests/identity/test_permission_scope.py.
        "identity_rolegrant",
        "identity_careteammembership",
        "identity_professionalregistration",
        # Exact clinical predicates and ACLs are checked in test_encounters.
        # Exact append-only history policies/ACLs: test_clinical_history.
        "ehr_historyassessment",
        "ehr_problem",
        "ehr_allergy",
        "ehr_specialtytemplate",
        "ehr_encounter",
        "ehr_clinicaldocument",
        "ehr_clinicaldocumentversion",
        "ehr_encounterintakereference",
        # Exact quarantine policies and ACLs are checked in test_attachments.
        "ehr_clinicalattachment",
        # Task-32 author-only drafts and snapshots: test_prescription_drafts.
        "prescription_prescriptiondraft",
        "prescription_prescriptionitem",
        # Task-33/34 artifacts and signing lifecycle: test_document_artifacts
        # and test_signatures check exact policies and ACLs.
        "prescription_prescriptiondocument",
        "prescription_signatureoperation",
        "prescription_signaturecallback",
        # Task-35 release/revocation policies and ACLs are checked exactly
        # in test_prescription_drafts and test_document_verification.
        "prescription_prescriptiondocumentrelease",
        "prescription_prescriptiondocumentrevocation",
        "intake_questionnairetemplate",
        "intake_questionnaireresponse",
        "intake_questionnaireevent",
        # Task-26 immutable patient decisions are checked in test_consent.
        "consent_consenttext",
        "consent_consentacceptance",
        "consent_consentrevocation",
        "scheduling_patientbookingevent",
        # Task-18 manager/patient policies are checked exactly in test_waitlist.
        "scheduling_waitlistentry",
        "scheduling_waitlistoffer",
        # Retention policies/ACLs are checked exactly in test_retention.
        "retention_retentionpolicy",
        "retention_legalhold",
        "retention_recordrelease",
        "retention_recordexport",
        # Teleconsult session policies are checked in test_teleconsult_sessions.
        "teleconsult_teleconsultsession",
        "teleconsult_teleconsultroom",
        "teleconsult_teleconsultcredential",
        "teleconsult_teleconsultevent",
        # Exact clinic billing policies and ACLs are checked in test_invoices.
        "billing_invoice",
        "billing_invoicerevision",
        "billing_settlement",
        "billing_receipt",
        # Immutable synthetic operations/results: test_pix_adapter.
        "billing_pixoperation",
        "billing_pixcharge",
        # Append-only authenticated payment events: test_payment_reconciliation.
        "billing_paymentevent",
    }
    assert table_posture == {
        (table, True, True, "clinic_owner") for table in EXPECTED_TENANT_COLUMNS
    }
    assert policies == {
        (table, "tenant_isolation") for table in EXPECTED_TENANT_COLUMNS
    } | {
        ("scheduling_availabilityblock", "patient_booking_read"),
        ("scheduling_appointment", "patient_booking_read"),
        ("scheduling_appointment", "patient_booking_insert"),
        ("scheduling_appointment", "patient_booking_update"),
    }


def test_tenant_policies_are_public_permissive_all_and_fail_closed() -> None:
    # Given: the immutable foundation and versioned Phase 1A policy targets
    # When: PostgreSQL deparses every policy expression
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT policies.tablename, policies.permissive, policies.roles,
                   policies.cmd, policies.qual, policies.with_check
            FROM pg_catalog.pg_policies AS policies
            WHERE policies.schemaname = 'clinic_app'
              AND policies.tablename = ANY(%s)
              AND policies.policyname = 'tenant_isolation'
            ORDER BY policies.tablename
            """,
            [list(EXPECTED_TENANT_COLUMNS)],
        )
        policy_rows = cursor.fetchall()

    # Then: each policy has identical fail-closed USING and WITH CHECK clauses
    assert len(policy_rows) == len(EXPECTED_TENANT_COLUMNS)
    for table, permissive, roles, command, using, with_check in policy_rows:
        tenant_column = EXPECTED_TENANT_COLUMNS[table]
        expected_expression = (
            f"({tenant_column} = (NULLIF(current_setting("
            "'app.current_tenant'::text, true), ''::text))::uuid)"
        )
        assert permissive == "PERMISSIVE"
        assert roles == ["public"]
        assert command == "ALL"
        assert using == expected_expression
        assert with_check == expected_expression


def test_runtime_role_and_tenant_table_privileges_are_exact(
    app_database_url: str,
) -> None:
    # Given: a runtime connection and the expected tenant-table DML surface
    with psycopg.connect(app_database_url) as app_connection:
        runtime_posture = app_connection.execute(
            """
            SELECT current_user, role.rolsuper, role.rolbypassrls,
                   role.rolcreatedb
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = current_user
            """
        ).fetchone()

    # When: explicit table and sensitive-user grants are enumerated
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, privilege_type
            FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_app'
              AND table_schema = 'clinic_app'
              AND table_name = ANY(%s)
            """,
            [list(EXPECTED_TENANT_COLUMNS)],
        )
        tenant_grants = set(cursor.fetchall())
        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.role_column_grants
            WHERE grantee = 'clinic_app'
              AND table_schema = 'clinic_app'
              AND table_name = ANY(%s)
              AND privilege_type = 'UPDATE'
            """,
            [list(EXPECTED_TENANT_COLUMNS)],
        )
        tenant_column_updates = set(cursor.fetchall())
        cursor.execute(
            """
            SELECT privilege_type FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
              AND table_name = 'identity_user'
            UNION ALL
            SELECT privilege_type FROM information_schema.role_column_grants
            WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
              AND table_name = 'identity_user'
            """
        )
        user_grants = cursor.fetchall()
        cursor.execute(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreatedb
            FROM pg_catalog.pg_roles
            WHERE rolname = ANY(%s)
            """,
            [["clinic_owner", "clinic_app", "clinic_resolver", "clinic_super"]],
        )
        role_posture = set(cursor.fetchall())

    # Then: app is ordinary, tenant DML is narrow, and User is fully denied
    assert runtime_posture == ("clinic_app", False, False, False)
    assert tenant_grants == {
        (table, "SELECT") for table in SELECT_ONLY_RUNTIME_TABLES
    } | {
        (table, privilege)
        for table in SELECT_INSERT_RUNTIME_TABLES
        for privilege in ("SELECT", "INSERT")
    } | {
        ("tenancy_tenantprobe", privilege)
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
    }
    assert user_grants == []
    assert tenant_column_updates == {
        ("comms_integrationoperation", "attempt_count"),
        ("comms_integrationoperation", "last_callback_event_id"),
        ("comms_integrationoperation", "last_error"),
        ("comms_integrationoperation", "provider_reference"),
        ("comms_integrationoperation", "status"),
        ("comms_integrationoperation", "updated_at"),
        ("intake_emergencycontact", "name"),
        ("intake_emergencycontact", "phone"),
        ("intake_emergencycontact", "relationship"),
        ("intake_emergencycontact", "retired_at"),
        ("intake_emergencycontact", "updated_at"),
        ("intake_emergencycontact", "version"),
        ("intake_insurancemembership", "ans_number"),
        ("intake_insurancemembership", "membership_number"),
        ("intake_insurancemembership", "payer_name"),
        ("intake_insurancemembership", "plan_name"),
        ("intake_insurancemembership", "retired_at"),
        ("intake_insurancemembership", "updated_at"),
        ("intake_insurancemembership", "valid_until"),
        ("intake_insurancemembership", "version"),
        ("intake_patient", "full_name"),
        ("intake_patientaddress", "city"),
        ("intake_patientaddress", "complement"),
        ("intake_patientaddress", "district"),
        ("intake_patientaddress", "postal_code"),
        ("intake_patientaddress", "retired_at"),
        ("intake_patientaddress", "state_code"),
        ("intake_patientaddress", "street"),
        ("intake_patientaddress", "street_number"),
        ("intake_patientaddress", "updated_at"),
        ("intake_patientaddress", "version"),
        ("intake_patientchannelpreference", "opted_in"),
        ("intake_patientchannelpreference", "updated_at"),
        ("intake_patientchannelpreference", "version"),
        ("intake_patientidentifier", "blind_index"),
        ("intake_patientidentifier", "index_key_version"),
        ("intake_patientidentifier", "issuer"),
        ("intake_patientidentifier", "retired_at"),
        ("intake_patientidentifier", "updated_at"),
        ("intake_patientidentifier", "value"),
        ("intake_patientidentifier", "version"),
        ("intake_patientcontact", "destination"),
        ("intake_patientcontact", "destination_version"),
        ("intake_patientcontact", "updated_at"),
        ("intake_patientaccessgrant", "revoked_at"),
        ("intake_patientcontact", "verification_method"),
        ("intake_patientcontact", "verified_at"),
        ("intake_patientcontact", "verified_version"),
        ("intake_patientsession", "revoked_at"),
        ("scheduling_appointment", "cancellation_reason"),
        ("scheduling_appointment", "cancelled_at"),
        ("scheduling_appointment", "end_at"),
        ("scheduling_appointment", "start_at"),
        ("scheduling_appointment", "status"),
        ("scheduling_appointment", "updated_at"),
        ("scheduling_availabilityblock", "retired_at"),
        ("scheduling_availabilityblock", "updated_at"),
        ("tenancy_tenantprobe", "id"),
        ("tenancy_tenantprobe", "label"),
        ("tenancy_tenantprobe", "organization_id"),
    }
    assert role_posture == {
        ("clinic_owner", True, False, False, False),
        ("clinic_app", True, False, False, False),
        ("clinic_resolver", False, False, True, False),
        ("clinic_super", True, True, False, False),
    }


def test_user_preference_table_is_user_bound_without_delete() -> None:
    # Given: the per-user display preference table (design system v2)
    # When: its row security and runtime grants are read
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT class.relrowsecurity, class.relforcerowsecurity,
                   class.relowner::regrole::text
            FROM pg_catalog.pg_class AS class
            WHERE class.oid = 'clinic_app.identity_userpreference'::regclass
            """
        )
        posture = cursor.fetchone()
        cursor.execute(
            """
            SELECT policyname, roles FROM pg_catalog.pg_policies
            WHERE schemaname = 'clinic_app' AND tablename = 'identity_userpreference'
            """
        )
        policies = cursor.fetchall()
        cursor.execute(
            """
            SELECT privilege_type FROM information_schema.role_table_grants
            WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
              AND table_name = 'identity_userpreference'
            """
        )
        grants = {row[0] for row in cursor.fetchall()}

    # Then: forced RLS bound to the user GUC, and no DELETE or full UPDATE
    assert posture == (True, True, "clinic_owner")
    assert policies == [("userpreference_owner_only", ["clinic_app"])]
    assert grants == {"SELECT", "INSERT"}
