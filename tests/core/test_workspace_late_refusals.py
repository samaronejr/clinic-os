"""Execute service-recheck refusals after the real workspace gate and form parsing."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest
from apps.consent import views as consent_views
from apps.identity import clinic_settings_views
from apps.identity.current_context import CurrentActorError
from apps.intake import views as intake_views
from apps.intake.services import ContactInputError, PatientAccessDeniedError
from apps.retention import views as retention_views
from apps.retention.services import RetentionConflictError
from apps.scheduling import booking_views
from apps.scheduling import views as availability_views
from apps.scheduling.services import (
    AvailabilityAccessDeniedError,
    AvailabilityHasAppointmentsError,
)
from django.core.exceptions import ValidationError
from django.urls import reverse

from core.test_navigation import _client_for, _get, _post

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_patient_service_rechecks(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    cases = (
        (
            "intake:patient-create",
            "create_patient",
            PatientAccessDeniedError(),
            {
                "full_name": "Sintetico Refusal",
                "birth_date": "1990-01-01",
                "idempotency_key": str(uuid4()),
            },
        ),
        (
            "intake:patient-contacts",
            "verify_contact",
            ContactInputError(),
            {
                "action": "verify",
                "enrollment_id": str(uuid4()),
                "channel": "email",
                "expected_version": "1",
            },
        ),
        (
            "intake:patient-access",
            "access_overview",
            PatientAccessDeniedError(),
            {
                "action": "manage",
                "enrollment_id": str(uuid4()),
            },
        ),
    )
    for route, service, error, fields in cases:
        with monkeypatch.context() as patch:
            recheck = Mock(side_effect=error)
            patch.setattr(intake_views, service, recheck)
            response = _post(
                client,
                reverse(route, kwargs={"clinic_id": rbac_graph.clinic_a}),
                fields,
            )
            recheck.assert_called_once()
            assert response.status_code == 404
            assert not response.cookies


def test_scheduling_service_rechecks(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    clinic = {"clinic_id": rbac_graph.clinic_a}
    booking = reverse("scheduling:appointment-create", kwargs=clinic)
    assert _post(client, booking, {"mode": "create"}).status_code == 404
    assert (
        _post(
            client, booking, {"mode": "prepare", "enrollment_id": str(uuid4())}
        ).status_code
        == 404
    )
    with monkeypatch.context() as patch:
        recheck = Mock(side_effect=AvailabilityAccessDeniedError())
        patch.setattr(availability_views, "create_availability", recheck)
        response = _post(
            client,
            reverse("scheduling:availability-list", kwargs=clinic),
            {
                "practitioner": str(rbac_graph.physician),
                "local_date": "2035-06-04",
                "start_time": "09:00",
                "end_time": "10:00",
                "idempotency_key": str(uuid4()),
            },
        )
        recheck.assert_called_once()
        assert response.status_code == 404
    with monkeypatch.context() as patch:
        patch.setattr(
            availability_views,
            "retire_availability",
            Mock(side_effect=AvailabilityHasAppointmentsError()),
        )
        recheck = Mock(side_effect=AvailabilityAccessDeniedError())
        patch.setattr(availability_views, "authorized_screen", recheck)
        response = _post(
            client,
            reverse(
                "scheduling:availability-retire",
                kwargs={**clinic, "availability_id": uuid4()},
            ),
            {},
        )
        recheck.assert_called_once()
        assert response.status_code == 404
    # This branch translates either scheduling authority failure identically.
    with monkeypatch.context() as patch:
        recheck = Mock(side_effect=AvailabilityAccessDeniedError())
        patch.setattr(booking_views, "prepare_booking", recheck)
        assert (
            _post(
                client, booking, {"mode": "prepare", "enrollment_id": str(uuid4())}
            ).status_code
            == 404
        )
        recheck.assert_called_once()


def test_retention_validation_and_conflict_exits(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client_for(rbac_graph, "clinic_admin")
    url = reverse("retention:status", kwargs={"clinic_id": rbac_graph.clinic_a})
    cases = (
        (
            "propose_policy",
            ValidationError("synthetic"),
            400,
            {
                "record_class": "ehr.document_version",
                "retention_days": "30",
            },
        ),
        (
            "place_hold",
            ValidationError("synthetic"),
            400,
            {
                "record_class": "ehr.document_version",
                "record_id": str(uuid4()),
                "authority": "Sintetico",
                "reason": "Sintetico",
            },
        ),
        (
            "release_hold",
            ValidationError("synthetic"),
            400,
            {
                "hold_id": str(uuid4()),
                "release_authority": "Sintetico",
                "release_reason": "Sintetico",
            },
        ),
        (
            "approve_policy",
            RetentionConflictError("precondition_failed"),
            409,
            {"policy_id": str(uuid4())},
        ),
    )
    for action, error, status, fields in cases:
        with monkeypatch.context() as patch:
            recheck = Mock(side_effect=error)
            patch.setattr(retention_views, action, recheck)
            response = _post(client, url, {"action": action, **fields})
            recheck.assert_called_once()
            assert response.status_code == status
    # A well-formed but unknown clinical version reaches the clinical refusal.
    physician, _ = _client_for(rbac_graph, "physician")
    assert (
        _post(
            physician, url, {"action": "release", "version_id": str(uuid4())}
        ).status_code
        == 403
    )


def test_staff_scope_rechecked_after_workspace_gate(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client_for(rbac_graph, "clinic_admin")
    for module, route in (
        (consent_views, "consent:staff"),
        (clinic_settings_views, "identity:clinic-logo"),
    ):
        with monkeypatch.context() as patch:
            recheck = Mock(side_effect=CurrentActorError())
            patch.setattr(module, "require_current_actor_clinic_roles", recheck)
            response = _get(
                client, reverse(route, kwargs={"clinic_id": rbac_graph.clinic_a})
            )
            recheck.assert_called_once()
            assert response.status_code == 403
            assert not response.cookies
