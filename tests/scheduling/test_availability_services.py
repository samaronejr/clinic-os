from __future__ import annotations

import importlib
from datetime import UTC, datetime
from inspect import Parameter, signature
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scheduling.models import AvailabilityBlock
from apps.tenancy.db import tenant_context
from django.db import connection

from patient_service_support import runtime_role
from scheduling.availability_service_support import (
    ExpectedBlock,
    assert_created_block,
    create_synthetic_block,
)

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_public_availability_services_derive_context_from_keyword_only_inputs() -> None:
    services = importlib.import_module("apps.scheduling.services")
    expected = {
        services.create_availability: [
            "clinic_id",
            "practitioner_id",
            "start_local",
            "end_local",
            "idempotency_key",
        ],
        services.view_availability: ["clinic_id"],
        services.retire_availability: ["clinic_id", "availability_id"],
    }
    for service, parameter_names in expected.items():
        parameters = signature(service).parameters
        assert list(parameters) == parameter_names
        assert all(
            value.kind is Parameter.KEYWORD_ONLY for value in parameters.values()
        )


def test_receptionist_creates_availability_with_one_metadata_audit(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.scheduling.services")
    create_availability = getattr(services, "create_availability", None)
    assert callable(create_availability)
    idempotency_key = uuid4()

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        block = create_availability(
            clinic_id=rbac_graph.clinic_a,
            practitioner_id=rbac_graph.physician,
            start_local="2035-01-02T09:00",
            end_local="2035-01-02T10:00",
            idempotency_key=idempotency_key,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_type, affected_record_id, "
                "(SELECT count(*) FROM jsonb_object_keys(payload)), "
                "payload ->> 'clinic_id', payload ->> 'object_verb' "
                "FROM clinic_app.audit_event_tenant ORDER BY seq"
            )
            audit_rows = cursor.fetchall()

    assert_created_block(
        block,
        ExpectedBlock(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            practitioner_id=rbac_graph.physician,
            idempotency_key=idempotency_key,
            expected_start=datetime(2035, 1, 2, 12, 0, tzinfo=UTC),
            expected_end=datetime(2035, 1, 2, 13, 0, tzinfo=UTC),
            start_utc="2035-01-02T12:00:00Z",
            end_utc="2035-01-02T13:00:00Z",
        ),
    )
    assert block.retired_at is None
    assert audit_rows == [
        (
            "scheduling.availability.created",
            "scheduling.availability",
            str(block.pk),
            2,
            str(rbac_graph.clinic_a),
            "created",
        )
    ]


def test_manager_views_active_clinic_availability_in_stable_order(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.scheduling.services")
    create_availability = getattr(services, "create_availability", None)
    view_availability = getattr(services, "view_availability", None)
    assert callable(create_availability)
    assert callable(view_availability)

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        later = create_synthetic_block(
            rbac_graph.clinic_a,
            rbac_graph.physician,
            ("2035-01-02", "10:00", "11:00"),
        )
        earlier = create_synthetic_block(
            rbac_graph.clinic_a,
            rbac_graph.physician,
            ("2035-01-02", "09:00", "10:00"),
        )
        items = view_availability(clinic_id=rbac_graph.clinic_a)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_type, affected_record_id, "
                "payload ->> 'clinic_id', payload ->> 'object_verb' "
                "FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.availability.viewed'"
            )
            view_events = cursor.fetchall()

    assert [item.availability_id for item in items] == [earlier.pk, later.pk]
    assert [item.practitioner_id for item in items] == [
        rbac_graph.physician,
        rbac_graph.physician,
    ]
    assert [item.start_at for item in items] == [earlier.start_at, later.start_at]
    assert view_events == [
        (
            "scheduling.availability.viewed",
            "identity.clinic",
            str(rbac_graph.clinic_a),
            str(rbac_graph.clinic_a),
            "viewed",
        )
    ]


def test_retirement_is_an_audited_noop_safe_state_change(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.scheduling.services")
    create_availability = getattr(services, "create_availability", None)
    retire_availability = getattr(services, "retire_availability", None)
    assert callable(create_availability)
    assert callable(retire_availability)

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        original = create_synthetic_block(
            rbac_graph.clinic_a,
            rbac_graph.physician,
            ("2035-01-03", "09:00", "10:00"),
        )
        retired = retire_availability(
            clinic_id=rbac_graph.clinic_a,
            availability_id=original.pk,
        )
        replay = retire_availability(
            clinic_id=rbac_graph.clinic_a,
            availability_id=original.pk,
        )
        replacement = create_synthetic_block(
            rbac_graph.clinic_a,
            rbac_graph.physician,
            ("2035-01-03", "09:30", "10:30"),
        )
        stored = AvailabilityBlock.objects.get(pk=original.pk)
        block_count = AvailabilityBlock.objects.count()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_type, affected_record_id, "
                "(SELECT count(*) FROM jsonb_object_keys(payload)), "
                "payload ->> 'clinic_id', payload ->> 'object_verb' "
                "FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.availability.retired'"
            )
            retire_events = cursor.fetchall()

    assert retired.pk == original.pk
    assert replay.pk == original.pk
    assert retired.retired_at is not None
    assert replay.retired_at == retired.retired_at
    assert stored.retired_at == retired.retired_at
    assert replacement.pk != original.pk
    assert block_count == 2
    assert retire_events == [
        (
            "scheduling.availability.retired",
            "scheduling.availability",
            str(original.pk),
            2,
            str(rbac_graph.clinic_a),
            "retired",
        )
    ]


def test_mismatch_and_active_overlap_conflict_while_adjacency_is_accepted(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.scheduling.services")
    conflict_error = getattr(services, "AvailabilityIdempotencyConflictError", None)
    overlap_error = getattr(services, "AvailabilityOverlapError", None)
    assert isinstance(conflict_error, type)
    assert issubclass(conflict_error, Exception)
    assert isinstance(overlap_error, type)
    assert issubclass(overlap_error, Exception)
    idempotency_key = uuid4()

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        create_synthetic_block(
            rbac_graph.clinic_a,
            rbac_graph.physician,
            ("2035-01-05", "09:00", "10:00"),
            idempotency_key=idempotency_key,
        )
        with pytest.raises(conflict_error, match="idempotency conflict"):
            create_synthetic_block(
                rbac_graph.clinic_a,
                rbac_graph.physician,
                ("2035-01-05", "09:00", "10:30"),
                idempotency_key=idempotency_key,
            )
        adjacent = create_synthetic_block(
            rbac_graph.clinic_a,
            rbac_graph.physician,
            ("2035-01-05", "10:00", "11:00"),
        )
        with pytest.raises(overlap_error, match="availability overlaps"):
            create_synthetic_block(
                rbac_graph.clinic_a,
                rbac_graph.physician,
                ("2035-01-05", "09:30", "10:30"),
            )
        block_count = AvailabilityBlock.objects.count()
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            audit_count = cursor.fetchone()

    assert adjacent.start_at == datetime(2035, 1, 5, 13, 0, tzinfo=UTC)
    assert block_count == 2
    assert audit_count == (2,)
