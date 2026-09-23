from __future__ import annotations

import importlib
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core.idempotency import create_fingerprint
from apps.tenancy.db import tenant_context
from django.db import connection

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_receptionist_creates_normalized_patient_and_enrollment_with_one_audit(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    create_patient = getattr(services, "create_patient", None)
    assert callable(create_patient)

    birth_date = date(2000, 1, 2)
    idempotency_key = uuid4()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="  A\N{COMBINING ACUTE ACCENT}na   Synthetic  ",
            birth_date=birth_date,
            idempotency_key=idempotency_key,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_type, affected_record_id, "
                "(SELECT count(*) FROM jsonb_object_keys(payload)), "
                "payload ->> 'clinic_id', "
                "payload ->> 'object_verb' "
                "FROM clinic_app.audit_event_tenant ORDER BY seq"
            )
            audit_rows = cursor.fetchall()

    assert registration.patient.full_name == "Ána Synthetic"
    assert registration.patient.birth_date == birth_date
    assert registration.patient.organization_id == rbac_graph.organization_a
    assert registration.enrollment.organization_id == rbac_graph.organization_a
    assert registration.enrollment.clinic_id == rbac_graph.clinic_a
    assert registration.enrollment.patient_id == registration.patient.pk
    assert registration.enrollment.idempotency_key == idempotency_key
    assert bytes(registration.enrollment.create_fingerprint) == create_fingerprint(
        "patient",
        {
            "birth_date": "2000-01-02",
            "clinic_id": str(rbac_graph.clinic_a),
            "full_name": "Ána Synthetic",
        },
    )
    assert audit_rows == [
        (
            "intake.patient.created",
            "intake.patient",
            str(registration.patient.pk),
            2,
            str(rbac_graph.clinic_a),
            "created",
        )
    ]


def test_distinct_keys_allow_duplicates_and_equal_replay_is_a_noop(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    create_patient = getattr(services, "create_patient", None)
    assert callable(create_patient)
    first_key = uuid4()

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        first = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Ana Synthetic",
            birth_date=date(2000, 1, 2),
            idempotency_key=first_key,
        )
        duplicate = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="  Ana   Synthetic ",
            birth_date=date(2000, 1, 2),
            idempotency_key=uuid4(),
        )
        replay = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Ana Synthetic",
            birth_date=date(2000, 1, 2),
            idempotency_key=first_key,
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.intake_patient")
            patient_count = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM clinic_app.intake_patientclinicenrollment"
            )
            enrollment_count = cursor.fetchone()
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            audit_count = cursor.fetchone()

    assert duplicate.patient.pk != first.patient.pk
    assert duplicate.enrollment.pk != first.enrollment.pk
    assert replay.patient.pk == first.patient.pk
    assert replay.enrollment.pk == first.enrollment.pk
    assert patient_count == (2,)
    assert enrollment_count == (2,)
    assert audit_count == (2,)


def test_failed_first_attempt_leaves_key_reusable_and_mismatch_conflicts(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    create_patient = getattr(services, "create_patient", None)
    birth_date_error = getattr(services, "PatientBirthDateError", None)
    conflict_error = getattr(services, "PatientIdempotencyConflictError", None)
    assert callable(create_patient)
    assert isinstance(birth_date_error, type)
    assert issubclass(birth_date_error, Exception)
    assert isinstance(conflict_error, type)
    assert issubclass(conflict_error, Exception)
    idempotency_key = uuid4()

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        with pytest.raises(birth_date_error, match="birth date is invalid"):
            create_patient(
                clinic_id=rbac_graph.clinic_a,
                full_name="Ana Synthetic",
                birth_date=date.max,
                idempotency_key=idempotency_key,
            )
        accepted = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Ana Synthetic",
            birth_date=date(2000, 1, 2),
            idempotency_key=idempotency_key,
        )
        with pytest.raises(conflict_error, match="idempotency conflict"):
            create_patient(
                clinic_id=rbac_graph.clinic_a,
                full_name="Bea Synthetic",
                birth_date=date(2000, 1, 2),
                idempotency_key=idempotency_key,
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.intake_patient")
            patient_count = cursor.fetchone()
            cursor.execute(
                "SELECT count(*) FROM clinic_app.intake_patientclinicenrollment"
            )
            enrollment_count = cursor.fetchone()
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            audit_count = cursor.fetchone()

    assert accepted.enrollment.idempotency_key == idempotency_key
    assert patient_count == (1,)
    assert enrollment_count == (1,)
    assert audit_count == (1,)


def test_invalid_normalized_names_leave_the_create_key_reusable(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    create_patient = getattr(services, "create_patient", None)
    input_error = getattr(services, "PatientCreateInputError", None)
    assert callable(create_patient)
    assert isinstance(input_error, type)
    assert issubclass(input_error, Exception)
    idempotency_key = uuid4()

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        for invalid_name in ("Synthetic\x00 Name", "Synthetic\ud800 Name"):
            with pytest.raises(input_error, match="create input is invalid"):
                create_patient(
                    clinic_id=rbac_graph.clinic_a,
                    full_name=invalid_name,
                    birth_date=date(2000, 1, 2),
                    idempotency_key=idempotency_key,
                )
        accepted = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Ana Synthetic",
            birth_date=date(2000, 1, 2),
            idempotency_key=idempotency_key,
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.intake_patient")
            patient_count = cursor.fetchone()
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            audit_count = cursor.fetchone()

    assert accepted.enrollment.idempotency_key == idempotency_key
    assert patient_count == (1,)
    assert audit_count == (1,)
