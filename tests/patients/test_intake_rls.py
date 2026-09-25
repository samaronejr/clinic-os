import os
from datetime import date
from importlib import import_module
from importlib.util import find_spec
from uuid import UUID

import psycopg
import pytest
from apps.identity.models import Clinic, Organization
from apps.intake.models import Patient, PatientClinicEnrollment
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from psycopg.errors import (
    InsufficientPrivilege,
    InvalidTextRepresentation,
)

from database_urls import database_url_for_name
from patients.test_intake_migrations import _default_connection, _scratch_database
from tenant_key_support import issue_tenant_key_for

ORG_A = UUID(int=2001)
ORG_B = UUID(int=2002)
CLINIC_A = UUID(int=2101)
CLINIC_B = UUID(int=2102)
CLINIC_FOREIGN = UUID(int=2103)
PATIENT_A = UUID(int=2201)
PATIENT_B = UUID(int=2202)
ENROLLMENT_A = UUID(int=2301)
ENROLLMENT_B = UUID(int=2302)
ENROLLMENT_FOREIGN = UUID(int=2303)


def _set_owner_tenant(organization_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
            [str(organization_id)],
        )


def _set_local(connection_: psycopg.Connection, value: str) -> None:
    connection_.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
        (value,),
    )


def _seed_rows() -> None:
    _set_owner_tenant(ORG_A)
    organization_a = Organization.objects.create(
        id=ORG_A,
        name="Synthetic Organization A",
        cnpj="00000000002001",
    )
    issue_tenant_key_for(ORG_A)
    clinic_a = Clinic.objects.create(
        id=CLINIC_A,
        organization=organization_a,
        name="Synthetic Clinic A",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
    )
    clinic_b = Clinic.objects.create(
        id=CLINIC_B,
        organization=organization_a,
        name="Synthetic Clinic B",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
    )
    patient_a = Patient.objects.create(
        id=PATIENT_A,
        organization=organization_a,
        full_name="Synthetic Patient A",
        birth_date=date(2000, 1, 2),
    )
    PatientClinicEnrollment.objects.create(
        id=ENROLLMENT_A,
        organization=organization_a,
        clinic=clinic_a,
        patient=patient_a,
        idempotency_key=UUID(int=2401),
        create_fingerprint=b"a" * 32,
    )
    PatientClinicEnrollment.objects.create(
        id=ENROLLMENT_B,
        organization=organization_a,
        clinic=clinic_b,
        patient=patient_a,
        idempotency_key=UUID(int=2402),
        create_fingerprint=b"b" * 32,
    )

    _set_owner_tenant(ORG_B)
    organization_b = Organization.objects.create(
        id=ORG_B,
        name="Synthetic Organization B",
        cnpj="00000000002002",
    )
    issue_tenant_key_for(ORG_B)
    clinic_foreign = Clinic.objects.create(
        id=CLINIC_FOREIGN,
        organization=organization_b,
        name="Synthetic Clinic Foreign",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
    )
    patient_b = Patient.objects.create(
        id=PATIENT_B,
        organization=organization_b,
        full_name="Synthetic Patient B",
        birth_date=date(2001, 1, 2),
    )
    PatientClinicEnrollment.objects.create(
        id=ENROLLMENT_FOREIGN,
        organization=organization_b,
        clinic=clinic_foreign,
        patient=patient_b,
        idempotency_key=UUID(int=2403),
        create_fingerprint=b"c" * 32,
    )


@pytest.mark.django_db(transaction=True)
def test_intake_tables_are_fail_closed_with_exact_runtime_acls(  # noqa: PLR0915 - one pass over the ACL matrix
    superuser_database_url: str,
) -> None:
    module = import_module("apps.intake.rls") if find_spec("apps.intake.rls") else None

    assert module is not None
    expected_targets = {
        ("intake_clinicintakepolicy", "organization_id"),
        ("intake_demographicscorrection", "organization_id"),
        ("intake_emergencycontact", "organization_id"),
        ("intake_insurancemembership", "organization_id"),
        ("intake_patient", "organization_id"),
        ("intake_patientaccessgrant", "organization_id"),
        ("intake_patientaddress", "organization_id"),
        ("intake_patientchannelpreference", "organization_id"),
        ("intake_patientclinicenrollment", "organization_id"),
        ("intake_patientcontact", "organization_id"),
        ("intake_patientcontactevent", "organization_id"),
        ("intake_patientdemographics", "organization_id"),
        ("intake_patientidentifier", "organization_id"),
        ("intake_patientsession", "organization_id"),
    }
    assert frozenset(expected_targets) == module.INTAKE_RLS_TARGETS
    # The protected-field migration is irreversible, so the unapply/reapply
    # cycle runs on a scratch database migrated forward to the last
    # reversible intake migration: the unapply plan then contains only
    # reversible migrations.
    with (
        _scratch_database(superuser_database_url) as wrapper,
        _default_connection(wrapper),
    ):
        app_database_url = database_url_for_name(
            os.environ["APP_DATABASE_URL"], str(wrapper.settings_dict["NAME"])
        )
        MigrationExecutor(connection).migrate(
            [
                (
                    "intake",
                    "0010_remove_patientaccessgrant_intake_grant_operations_check_and_more",
                )
            ]
        )
        MigrationExecutor(connection).migrate([("intake", None)])
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT class.relname, class.relrowsecurity,
                       class.relforcerowsecurity, class.relowner::regrole::text
                FROM pg_catalog.pg_class AS class
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname = 'clinic_app'
                  AND class.relname = ANY(%s)
                ORDER BY class.relname
                """,
                [[table for table, _ in sorted(expected_targets)]],
            )
            assert cursor.fetchall() == [
                (table, True, True, "clinic_owner")
                for table in sorted(table for table, _ in expected_targets)
            ]
            cursor.execute(
                """
                SELECT tablename, policyname, permissive, roles, cmd, qual,
                       with_check
                FROM pg_catalog.pg_policies
                WHERE schemaname = 'clinic_app' AND tablename = ANY(%s)
                ORDER BY tablename, policyname
                """,
                [[table for table, _ in sorted(expected_targets)]],
            )
            policy_rows = cursor.fetchall()
            assert len(policy_rows) == len(expected_targets)
            for table, policy, permissive, roles, command, using, check in policy_rows:
                assert policy == "tenant_isolation"
                assert permissive == "PERMISSIVE"
                assert roles == ["public"]
                assert command == "ALL"
                expected = (
                    "(organization_id = (NULLIF(current_setting("
                    "'app.current_tenant'::text, true), ''::text))::uuid)"
                )
                assert using == check == expected, table
            cursor.execute(
                """
                SELECT table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE grantee = 'clinic_app' AND table_schema = 'clinic_app'
                  AND table_name = ANY(%s)
                ORDER BY table_name, privilege_type
                """,
                [[table for table, _ in sorted(expected_targets)]],
            )
            assert cursor.fetchall() == [
                ("intake_clinicintakepolicy", "INSERT"),
                ("intake_clinicintakepolicy", "SELECT"),
                ("intake_demographicscorrection", "INSERT"),
                ("intake_demographicscorrection", "SELECT"),
                ("intake_emergencycontact", "INSERT"),
                ("intake_emergencycontact", "SELECT"),
                ("intake_insurancemembership", "INSERT"),
                ("intake_insurancemembership", "SELECT"),
                ("intake_patient", "INSERT"),
                ("intake_patient", "SELECT"),
                ("intake_patientaccessgrant", "INSERT"),
                ("intake_patientaccessgrant", "SELECT"),
                ("intake_patientaddress", "INSERT"),
                ("intake_patientaddress", "SELECT"),
                ("intake_patientchannelpreference", "INSERT"),
                ("intake_patientchannelpreference", "SELECT"),
                ("intake_patientclinicenrollment", "INSERT"),
                ("intake_patientclinicenrollment", "SELECT"),
                ("intake_patientcontact", "INSERT"),
                ("intake_patientcontact", "SELECT"),
                ("intake_patientcontactevent", "INSERT"),
                ("intake_patientcontactevent", "SELECT"),
                ("intake_patientdemographics", "INSERT"),
                ("intake_patientdemographics", "SELECT"),
                ("intake_patientidentifier", "INSERT"),
                ("intake_patientidentifier", "SELECT"),
                ("intake_patientsession", "SELECT"),
            ]

        _seed_rows()
        with psycopg.connect(app_database_url) as app_connection:
            _set_local(app_connection, str(ORG_A))
            assert app_connection.execute(
                "SELECT id FROM clinic_app.intake_patient ORDER BY id"
            ).fetchall() == [(PATIENT_A,)]
            assert app_connection.execute(
                "SELECT id FROM clinic_app.intake_patientclinicenrollment "
                "WHERE clinic_id = %s ORDER BY id",
                (CLINIC_A,),
            ).fetchall() == [(ENROLLMENT_A,)]
            assert (
                app_connection.execute(
                    "SELECT id FROM clinic_app.intake_patientclinicenrollment "
                    "WHERE clinic_id = %s ORDER BY id",
                    (CLINIC_FOREIGN,),
                ).fetchall()
                == []
            )
            # full_name and birth_date are tenant envelopes: raw inserts
            # carry opaque bytes, so plaintext-shape checks no longer exist
            # at the database boundary; normalization is enforced by the
            # field layer.
            app_connection.execute(
                "INSERT INTO clinic_app.intake_patient "
                "(id, organization_id, full_name, birth_date, created_at) "
                "VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)",
                [
                    UUID(int=2501),
                    ORG_A,
                    psycopg.Binary(b"\x01synthetic-envelope"),
                    psycopg.Binary(b"\x01synthetic-envelope"),
                ],
            )
            app_connection.rollback()
            _set_local(app_connection, str(ORG_A))
            # Legal-name corrections mirror back onto the registry row: the
            # runtime role may UPDATE only the full_name column.
            app_connection.execute(
                "UPDATE clinic_app.intake_patient SET full_name = full_name"
            )
            app_connection.rollback()
            _set_local(app_connection, str(ORG_A))
            with pytest.raises(InsufficientPrivilege):
                app_connection.execute(
                    "UPDATE clinic_app.intake_patient SET birth_date = birth_date"
                )
            app_connection.rollback()
            _set_local(app_connection, str(ORG_A))
            with pytest.raises(InsufficientPrivilege):
                app_connection.execute("DELETE FROM clinic_app.intake_patient")
            app_connection.rollback()
            _set_local(app_connection, str(ORG_A))
            with pytest.raises(InsufficientPrivilege):
                app_connection.execute(
                    "INSERT INTO clinic_app.intake_patient "
                    "(id, organization_id, full_name, birth_date, created_at) "
                    "VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)",
                    [
                        UUID(int=2502),
                        ORG_B,
                        psycopg.Binary(b"\x01synthetic-envelope"),
                        psycopg.Binary(b"\x01synthetic-envelope"),
                    ],
                )
            app_connection.rollback()

        with psycopg.connect(app_database_url) as app_connection:
            assert app_connection.execute(
                "SELECT count(*) FROM clinic_app.intake_patient"
            ).fetchone() == (0,)
            _set_local(app_connection, "not-a-uuid")
            with pytest.raises(InvalidTextRepresentation):
                app_connection.execute("SELECT count(*) FROM clinic_app.intake_patient")
            app_connection.rollback()
