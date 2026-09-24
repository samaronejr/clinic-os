from __future__ import annotations

import importlib
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_create_denies_physician_unassigned_and_foreign_clinics_without_writes(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    create_patient = getattr(services, "create_patient", None)
    access_error = getattr(services, "PatientAccessDeniedError", None)
    assert callable(create_patient)
    assert isinstance(access_error, type)
    assert issubclass(access_error, Exception)

    denied_calls = (
        (rbac_graph.physician, rbac_graph.clinic_a),
        (rbac_graph.shared_user, rbac_graph.clinic_b),
        (rbac_graph.shared_user, rbac_graph.clinic_c),
    )
    for actor_id, clinic_id in denied_calls:
        with (
            runtime_role(),
            tenant_context(actor_id, rbac_graph.organization_a),
        ):
            with pytest.raises(access_error, match="patient access denied"):
                create_patient(
                    clinic_id=clinic_id,
                    full_name="Ana Synthetic",
                    birth_date=date(2000, 1, 2),
                    idempotency_key=uuid4(),
                )
            with connection.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM clinic_app.intake_patient")
                assert cursor.fetchone() == (0,)
                cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
                assert cursor.fetchone() == (0,)


def test_create_denies_inactive_missing_and_malformed_actors(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    create_patient = getattr(services, "create_patient", None)
    access_error = getattr(services, "PatientAccessDeniedError", None)
    assert callable(create_patient)
    assert isinstance(access_error, type)
    assert issubclass(access_error, Exception)
    User.objects.filter(pk=rbac_graph.shared_user).update(is_active=False)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        pytest.raises(access_error, match="patient access denied"),
    ):
        create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Ana Synthetic",
            birth_date=date(2000, 1, 2),
            idempotency_key=uuid4(),
        )

    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        for raw_actor in ("", "not-a-uuid"):
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
                [raw_actor],
            )
            with pytest.raises(access_error, match="patient access denied"):
                create_patient(
                    clinic_id=rbac_graph.clinic_a,
                    full_name="Ana Synthetic",
                    birth_date=date(2000, 1, 2),
                    idempotency_key=uuid4(),
                )


def test_equal_replay_survives_one_manager_role_removal(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    create_patient = getattr(services, "create_patient", None)
    assert callable(create_patient)
    idempotency_key = uuid4()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.shared_user,
            role=UserClinicRole.Role.OWNER,
        )

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        first = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Ana Synthetic",
            birth_date=date(2000, 1, 2),
            idempotency_key=idempotency_key,
        )

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.filter(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.shared_user,
            role=UserClinicRole.Role.RECEPTIONIST,
        ).delete()

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        replay = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Ana Synthetic",
            birth_date=date(2000, 1, 2),
            idempotency_key=idempotency_key,
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            audit_count = cursor.fetchone()

    assert replay.patient.pk == first.patient.pk
    assert replay.enrollment.pk == first.enrollment.pk
    assert audit_count == (1,)
