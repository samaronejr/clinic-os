from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_search_returns_only_selected_clinic_in_deterministic_order(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    create_patient = getattr(services, "create_patient", None)
    search_patients = getattr(services, "search_patients", None)
    assert callable(create_patient)
    assert callable(search_patients)

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=rbac_graph.shared_user,
            role=UserClinicRole.Role.RECEPTIONIST,
        )

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        selected = [
            create_patient(
                clinic_id=rbac_graph.clinic_a,
                full_name=full_name,
                birth_date=birth_date,
                idempotency_key=uuid4(),
            )
            for full_name, birth_date in (
                ("zoe Synthetic", date(2000, 1, 2)),
                ("Ana Synthetic", date(2001, 1, 2)),
                ("ana Synthetic", date(1999, 1, 2)),
            )
        ]
        create_patient(
            clinic_id=rbac_graph.clinic_b,
            full_name="Aaa Synthetic",
            birth_date=date(1990, 1, 2),
            idempotency_key=uuid4(),
        )
        result = search_patients(
            clinic_id=rbac_graph.clinic_a,
            query="Synthetic",
            page=1,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_type, affected_record_id, "
                "(SELECT count(*) FROM jsonb_object_keys(payload)), "
                "payload ->> 'clinic_id', payload ->> 'object_verb' "
                "FROM clinic_app.audit_event_tenant ORDER BY seq DESC LIMIT 1"
            )
            search_audit = cursor.fetchone()

    expected = sorted(
        selected,
        key=lambda registration: (
            registration.patient.full_name.lower(),
            registration.patient.birth_date,
            registration.patient.pk,
        ),
    )
    assert [item.enrollment_id for item in result.items] == [
        registration.enrollment.pk for registration in expected
    ]
    assert [item.full_name for item in result.items] == [
        registration.patient.full_name for registration in expected
    ]
    assert result.page == 1
    assert result.total == 3
    assert result.page_count == 1
    assert search_audit == (
        "intake.patient.searched",
        "identity.clinic",
        str(rbac_graph.clinic_a),
        2,
        str(rbac_graph.clinic_a),
        "searched",
    )


def test_search_matches_accented_names_case_insensitively(
    rbac_graph: RbacGraph,
) -> None:
    """Decrypted names keep the database collation, so accents case-fold."""
    services = importlib.import_module("apps.intake.services")
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = services.create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Concei\u00e7\u00e3o Sint\u00e9tica",
            birth_date=date(1990, 1, 2),
            idempotency_key=uuid4(),
        )
        found = [
            services.search_patients(clinic_id=rbac_graph.clinic_a, query=query, page=1)
            for query in ("concei\u00e7\u00e3o", "SINT\u00c9TICA", "Sint\u00e9tica")
        ]
    for result in found:
        assert [item.enrollment_id for item in result.items] == [
            registration.enrollment.pk
        ]


def test_search_paginates_twenty_five_per_page_and_filters_exact_birth_date(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    create_patient = getattr(services, "create_patient", None)
    search_patients = getattr(services, "search_patients", None)
    assert callable(create_patient)
    assert callable(search_patients)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registrations = [
            create_patient(
                clinic_id=rbac_graph.clinic_a,
                full_name=f"Synthetic Person {index:02d}",
                birth_date=date(2000, 1, index % 3 + 1),
                idempotency_key=uuid4(),
            )
            for index in range(27)
        ]
        first_page = search_patients(
            clinic_id=rbac_graph.clinic_a,
            query="Person",
            page=1,
        )
        second_page = search_patients(
            clinic_id=rbac_graph.clinic_a,
            query="Person",
            page=2,
        )
        exact_date = search_patients(
            clinic_id=rbac_graph.clinic_a,
            query="Person",
            page=1,
            birth_date=date(2000, 1, 2),
        )
        empty = search_patients(
            clinic_id=rbac_graph.clinic_a,
            query="Missing Synthetic",
            page=1,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'intake.patient.searched'"
            )
            search_audit_count = cursor.fetchone()

    expected = sorted(
        registrations,
        key=lambda registration: (
            registration.patient.full_name.lower(),
            registration.patient.birth_date,
            registration.patient.pk,
        ),
    )
    assert [item.enrollment_id for item in first_page.items] == [
        registration.enrollment.pk for registration in expected[:25]
    ]
    assert [item.enrollment_id for item in second_page.items] == [
        registration.enrollment.pk for registration in expected[25:]
    ]
    assert first_page.total == 27
    assert first_page.page_count == 2
    assert second_page.page == 2
    assert len(second_page.items) == 2
    assert exact_date.total == 9
    assert all(item.birth_date == date(2000, 1, 2) for item in exact_date.items)
    assert empty.items == ()
    assert empty.total == 0
    assert empty.page_count == 0
    assert search_audit_count == (4,)


def test_search_validation_and_scope_failures_append_nothing(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    search_patients = getattr(services, "search_patients", None)
    input_error = getattr(services, "PatientSearchInputError", None)
    access_error = getattr(services, "PatientAccessDeniedError", None)
    assert callable(search_patients)
    assert isinstance(input_error, type)
    assert issubclass(input_error, Exception)
    assert isinstance(access_error, type)
    assert issubclass(access_error, Exception)

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        invalid_calls = (
            {"query": "x", "page": 1},
            {"query": "x" * 101, "page": 1},
            {"query": "x\x00", "page": 1},
            {"query": "x\ud800", "page": 1},
            {"query": "valid", "page": 0},
            {"query": "valid", "page": True},
            {
                "query": "valid",
                "page": 1,
                "birth_date": datetime(2000, 1, 2, tzinfo=UTC),
            },
        )
        for arguments in invalid_calls:
            with pytest.raises(input_error, match="search input is invalid"):
                search_patients(clinic_id=rbac_graph.clinic_a, **arguments)
        with pytest.raises(access_error, match="patient access denied"):
            search_patients(clinic_id=rbac_graph.clinic_b, query="valid", page=1)
        with pytest.raises(access_error, match="patient access denied"):
            search_patients(clinic_id=rbac_graph.clinic_c, query="valid", page=1)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            assert cursor.fetchone() == (0,)

    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        pytest.raises(access_error, match="patient access denied"),
    ):
        search_patients(clinic_id=rbac_graph.clinic_a, query="valid", page=1)
