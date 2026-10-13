"""Reach lifecycle refusals after the real workspace gate and input parsing."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest
from apps.scheduling import lifecycle_views
from apps.scheduling.appointment_lifecycle import (
    AppointmentLifecycleError,
    AppointmentLifecycleInputError,
)
from django.urls import reverse

from core.test_navigation import _client_for, _post

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_lifecycle_action_and_service_refusals(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    url = reverse(
        "scheduling:appointment-lifecycle",
        kwargs={"clinic_id": rbac_graph.clinic_a},
    )
    fields = {
        "appointment_id": str(uuid4()),
        "command_id": str(uuid4()),
        "expected_revision": "1",
        "action": "arrive",
    }
    response = _post(client, url, {**fields, "action": "invalid"})
    assert response.status_code == 404
    assert not response.cookies
    for error, status in (
        (AppointmentLifecycleInputError(), 404),
        (AppointmentLifecycleError("revision_conflict"), 409),
    ):
        with monkeypatch.context() as patch:
            recheck = Mock(side_effect=error)
            patch.setattr(lifecycle_views, "arrive", recheck)
            response = _post(client, url, fields)
            recheck.assert_called_once()
            assert response.status_code == status
            assert not response.cookies


def test_lifecycle_refuses_invalid_clinic_timezone(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client_for(rbac_graph, "receptionist")
    recheck = Mock(return_value=Mock())
    monkeypatch.setattr(lifecycle_views, "arrive", recheck)
    clinic_model = Mock()
    clinic_model.objects.get.return_value.timezone = None
    monkeypatch.setattr(lifecycle_views, "Clinic", clinic_model)
    response = _post(
        client,
        reverse(
            "scheduling:appointment-lifecycle",
            kwargs={"clinic_id": rbac_graph.clinic_a},
        ),
        {
            "appointment_id": str(uuid4()),
            "command_id": str(uuid4()),
            "expected_revision": "1",
            "action": "arrive",
        },
    )
    recheck.assert_called_once()
    clinic_model.objects.get.assert_called_once_with(pk=rbac_graph.clinic_a)
    assert response.status_code == 404
    assert not response.cookies
