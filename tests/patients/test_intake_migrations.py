from collections.abc import Sequence
from importlib import import_module
from importlib.util import find_spec
from uuid import UUID

import pytest
from apps.identity.models import Clinic, Organization
from apps.intake.models import Patient, PatientClinicEnrollment
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

MIGRATION_MODULE = "apps.intake.migrations.0001_patient_and_enrollment"
FOUNDATION_TARGETS = [
    ("identity", "0003_totp_device_rls"),
    ("tenancy", "0002_rls_and_resolvers"),
]
PRE_INTAKE_TARGETS = [
    ("identity", "0005_clinic_timezone"),
    ("tenancy", "0002_rls_and_resolvers"),
]
ORG_A = UUID(int=1001)
ORG_B = UUID(int=1002)
CLINIC_A = UUID(int=1101)
CLINIC_B = UUID(int=1102)
CLINIC_FOREIGN = UUID(int=1103)


def _migrate(targets: Sequence[tuple[str, str | None]]) -> None:
    MigrationExecutor(connection).migrate(targets)


def _migrate_head() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def _set_tenant(organization_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
            [str(organization_id)],
        )


@pytest.mark.django_db(transaction=True)
def test_intake_migration_preserves_duplicates_and_enrollment_integrity() -> None:
    module = import_module(MIGRATION_MODULE) if find_spec(MIGRATION_MODULE) else None

    assert module is not None
    assert module.Migration.dependencies == PRE_INTAKE_TARGETS
    try:
        _migrate(FOUNDATION_TARGETS)
        _migrate_head()
        _set_tenant(ORG_A)
        organization_a = Organization.objects.create(
            id=ORG_A,
            name="Synthetic Organization A",
            cnpj="00000000001001",
        )
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
        duplicate_a = Patient.objects.create(
            organization=organization_a,
            full_name="  Ana   Synthetic  ",
            birth_date="2000-01-02",
        )
        duplicate_b = Patient.objects.create(
            organization=organization_a,
            full_name="Ana Synthetic",
            birth_date="2000-01-02",
        )
        enrollment_a = PatientClinicEnrollment.objects.create(
            organization=organization_a,
            clinic=clinic_a,
            patient=duplicate_a,
            idempotency_key=UUID(int=1201),
            create_fingerprint=b"a" * 32,
        )
        enrollment_b = PatientClinicEnrollment.objects.create(
            organization=organization_a,
            clinic=clinic_b,
            patient=duplicate_a,
            idempotency_key=UUID(int=1202),
            create_fingerprint=b"b" * 32,
        )

        assert duplicate_a.full_name == duplicate_b.full_name == "Ana Synthetic"
        assert {enrollment_a.clinic_id, enrollment_b.clinic_id} == {
            CLINIC_A,
            CLINIC_B,
        }

        _set_tenant(ORG_B)
        organization_b = Organization.objects.create(
            id=ORG_B,
            name="Synthetic Organization B",
            cnpj="00000000001002",
        )
        Clinic.objects.create(
            id=CLINIC_FOREIGN,
            organization=organization_b,
            name="Synthetic Clinic Foreign",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        _set_tenant(ORG_A)
        with pytest.raises(IntegrityError), transaction.atomic():
            PatientClinicEnrollment.objects.create(
                organization=organization_a,
                clinic_id=CLINIC_FOREIGN,
                patient=duplicate_b,
                idempotency_key=UUID(int=1203),
                create_fingerprint=b"c" * 32,
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            PatientClinicEnrollment.objects.create(
                organization=organization_a,
                clinic=clinic_a,
                patient=duplicate_b,
                idempotency_key=UUID(int=1204),
                create_fingerprint=b"short",
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            PatientClinicEnrollment.objects.create(
                organization=organization_a,
                clinic=clinic_a,
                patient=duplicate_a,
                idempotency_key=UUID(int=1205),
                create_fingerprint=b"d" * 32,
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            PatientClinicEnrollment.objects.create(
                organization=organization_a,
                clinic=clinic_b,
                patient=duplicate_b,
                idempotency_key=enrollment_a.idempotency_key,
                create_fingerprint=b"e" * 32,
            )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname FROM pg_catalog.pg_constraint "
                "WHERE conrelid = 'clinic_app.intake_patientclinicenrollment'"
                "::regclass AND conname = ANY(%s) ORDER BY conname",
                [
                    [
                        "intake_enrollment_fingerprint_32_check",
                        "intake_enrollment_org_clinic_fk",
                        "intake_enrollment_org_patient_fk",
                    ]
                ],
            )
            assert [row[0] for row in cursor.fetchall()] == [
                "intake_enrollment_fingerprint_32_check",
                "intake_enrollment_org_clinic_fk",
                "intake_enrollment_org_patient_fk",
            ]

        _migrate([("intake", None)])
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('clinic_app.intake_patient')")
            assert cursor.fetchone() == (None,)
        _migrate_head()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('clinic_app.intake_patient'), "
                "to_regclass('clinic_app.intake_patientclinicenrollment')"
            )
            row = cursor.fetchone()
            assert row is not None
            assert all(value is not None for value in row)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET app.current_tenant")
        _migrate_head()
