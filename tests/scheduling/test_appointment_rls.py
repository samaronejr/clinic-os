from datetime import date, timedelta
from importlib import import_module
from uuid import UUID

import psycopg
import pytest
from apps.identity.models import Clinic, Organization, User
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.scheduling.models import Appointment, AvailabilityBlock
from psycopg import sql
from psycopg.errors import (
    ForeignKeyViolation,
    InsufficientPrivilege,
    InvalidTextRepresentation,
)

from scheduling.availability_test_support import END, ORG_ID, START, seed, set_tenant
from tenant_key_support import issue_tenant_key_for

ORG_B_ID = UUID(int=7401)
CLINIC_FOREIGN_ID = UUID(int=7402)
PRACTITIONER_FOREIGN_ID = UUID(int=7403)


def _require_appointment_rls() -> None:
    module = import_module("apps.scheduling.rls")
    appointment_targets = getattr(module, "APPOINTMENT_RLS_TARGETS", None)
    assert appointment_targets == frozenset(
        {("scheduling_appointment", "organization_id")}
    )
    assert module.SCHEDULING_RLS_TARGETS | appointment_targets == frozenset(
        {
            ("scheduling_appointment", "organization_id"),
            ("scheduling_availabilityblock", "organization_id"),
        }
    )


def _set_local(raw_connection: psycopg.Connection, organization_id: UUID) -> None:
    raw_connection.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
        (str(organization_id),),
    )


@pytest.mark.django_db(transaction=True)
def test_appointment_runtime_denies_tenant_clinic_immutable_and_delete_drift(
    app_database_url: str,
) -> None:
    _require_appointment_rls()
    organization, clinic_a, clinic_b, practitioner_a, practitioner_b = seed()
    patient = Patient.objects.create(
        organization=organization,
        full_name="Synthetic RLS Patient",
        birth_date=date(2000, 1, 2),
    )
    PatientClinicEnrollment.objects.create(
        organization=organization,
        clinic=clinic_a,
        patient=patient,
        idempotency_key=UUID(int=7411),
        create_fingerprint=b"a" * 32,
    )
    for offset, (clinic, practitioner) in enumerate(
        ((clinic_a, practitioner_a), (clinic_b, practitioner_b)),
        start=1,
    ):
        AvailabilityBlock.objects.create(
            organization=organization,
            clinic=clinic,
            practitioner=practitioner,
            start_at=START,
            end_at=END + timedelta(hours=2),
            idempotency_key=UUID(int=7420 + offset),
            create_fingerprint=bytes([offset]) * 32,
        )
    appointment = Appointment.objects.create(
        organization=organization,
        clinic=clinic_a,
        patient=patient,
        practitioner=practitioner_a,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=7431),
        create_fingerprint=b"b" * 32,
    )

    set_tenant(ORG_B_ID)
    organization_b = Organization.objects.create(
        id=ORG_B_ID,
        name="Synthetic Foreign Appointment Organization",
        cnpj="00000000007401",
    )
    issue_tenant_key_for(ORG_B_ID)
    clinic_foreign = Clinic.objects.create(
        id=CLINIC_FOREIGN_ID,
        organization=organization_b,
        name="Synthetic Foreign Appointment Clinic",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
    )
    practitioner_foreign = User.objects.create(
        id=PRACTITIONER_FOREIGN_ID,
        username="synthetic-foreign-appointment-practitioner",
    )
    patient_b = Patient.objects.create(
        organization=organization_b,
        full_name="Synthetic Foreign RLS Patient",
        birth_date=date(2001, 2, 3),
    )
    PatientClinicEnrollment.objects.create(
        organization=organization_b,
        clinic=clinic_foreign,
        patient=patient_b,
        idempotency_key=UUID(int=7432),
        create_fingerprint=b"c" * 32,
    )
    AvailabilityBlock.objects.create(
        organization=organization_b,
        clinic=clinic_foreign,
        practitioner=practitioner_foreign,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=7433),
        create_fingerprint=b"d" * 32,
    )
    foreign_appointment = Appointment.objects.create(
        organization=organization_b,
        clinic=clinic_foreign,
        patient=patient_b,
        practitioner=practitioner_foreign,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=7434),
        create_fingerprint=b"e" * 32,
    )

    with psycopg.connect(app_database_url) as app_connection:
        _set_local(app_connection, ORG_ID)
        assert app_connection.execute(
            "SELECT id FROM clinic_app.scheduling_appointment"
        ).fetchall() == [(appointment.pk,)]
        assert (
            app_connection.execute(
                "SELECT id FROM clinic_app.scheduling_appointment WHERE id=%s",
                (foreign_appointment.pk,),
            ).fetchall()
            == []
        )
        with pytest.raises(InsufficientPrivilege):
            app_connection.execute(
                "INSERT INTO clinic_app.scheduling_appointment "
                "(id,organization_id,clinic_id,patient_id,practitioner_id,start_at,"
                "end_at,idempotency_key,create_fingerprint,status,cancellation_reason,"
                "cancelled_at,created_at,updated_at) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,'cancelled','other',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (
                    UUID(int=7435),
                    ORG_B_ID,
                    clinic_foreign.pk,
                    patient_b.pk,
                    practitioner_foreign.pk,
                    START,
                    END,
                    UUID(int=7436),
                    b"f" * 32,
                ),
            )
        app_connection.rollback()
        _set_local(app_connection, ORG_ID)
        app_connection.execute(
            "UPDATE clinic_app.scheduling_appointment "
            "SET start_at=%s, end_at=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (END, END + timedelta(hours=1), appointment.pk),
        )
        app_connection.execute(
            "UPDATE clinic_app.scheduling_appointment SET status='cancelled', "
            "cancellation_reason='clinic_request', cancelled_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=%s",
            (appointment.pk,),
        )
        app_connection.commit()
        for column in (
            "organization_id",
            "clinic_id",
            "patient_id",
            "practitioner_id",
            "idempotency_key",
            "create_fingerprint",
        ):
            _set_local(app_connection, ORG_ID)
            statement = sql.SQL(
                "UPDATE clinic_app.scheduling_appointment "
                "SET {column}={column} WHERE id=%s"
            ).format(column=sql.Identifier(column))
            with pytest.raises(InsufficientPrivilege):
                app_connection.execute(statement, (appointment.pk,))
            app_connection.rollback()
        _set_local(app_connection, ORG_ID)
        with pytest.raises(InsufficientPrivilege):
            app_connection.execute(
                "DELETE FROM clinic_app.scheduling_appointment WHERE id=%s",
                (appointment.pk,),
            )
        app_connection.rollback()
        _set_local(app_connection, ORG_ID)
        with pytest.raises(ForeignKeyViolation):
            app_connection.execute(
                "INSERT INTO clinic_app.scheduling_appointment "
                "(id,organization_id,clinic_id,patient_id,practitioner_id,start_at,"
                "end_at,idempotency_key,create_fingerprint,status,created_at,"
                "updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'scheduled',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (
                    UUID(int=7441),
                    ORG_ID,
                    clinic_b.pk,
                    patient.pk,
                    practitioner_b.pk,
                    START,
                    END,
                    UUID(int=7442),
                    b"d" * 32,
                ),
            )
        app_connection.rollback()

    with psycopg.connect(app_database_url) as app_connection:
        assert app_connection.execute(
            "SELECT count(*) FROM clinic_app.scheduling_appointment"
        ).fetchone() == (0,)
        app_connection.execute(
            "SELECT pg_catalog.set_config('app.current_tenant','not-a-uuid',true)"
        )
        with pytest.raises(InvalidTextRepresentation):
            app_connection.execute(
                "SELECT count(*) FROM clinic_app.scheduling_appointment"
            )
