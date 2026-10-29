"""Closed device failures persist and render in the physician's patient summary.

The browser journey exercises the automatic measurement and FormData path;
these HTTP regressions cover its stored vocabulary and the native fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.teleconsult.models import TeleconsultDeviceCheck
from apps.tenancy.db import tenant_context

from accessible_document import Document
from patient_service_support import runtime_role
from renewal.test_patient_access import _patient_client
from renewal.test_retention import staff_client
from renewal.test_teleconsult_sessions import synthetic_provider
from teleconsult.test_participants import seed_world

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

__all__ = ("synthetic_provider",)
pytestmark = pytest.mark.django_db(transaction=True)
PATIENT_PATH = "/patient/teleconsult/"


@pytest.mark.parametrize("code", ["denied", "missing", "busy", "unsupported"])
def test_failure_codes_are_stored_and_rendered_for_the_physician(
    rbac_graph: RbacGraph, synthetic_provider: object, code: str
) -> None:
    w = seed_world(rbac_graph)
    results = {
        "camera": code,
        "microphone": code,
        "speaker": "unsupported" if code == "unsupported" else "not_checked",
        "network": "good",
    }
    patient = _patient_client(w.patient_session)
    with runtime_role():
        response = patient.post(
            PATIENT_PATH,
            {"action": "device_check", "session_id": str(w.session), **results},
            HTTP_ACCEPT="application/json",
        )
    assert response.status_code == 200
    assert response.json() == {"recorded": True}
    with runtime_role(), tenant_context(w.graph.physician, w.organization):
        check = TeleconsultDeviceCheck.objects.get(session_id=w.session)
        assert (check.role, check.participant_id) == (
            "patient",
            check.session.patient_id,
        )
        assert {field: getattr(check, field) for field in results} == results
    with staff_client(w.graph.physician) as physician:
        summary = physician.post(
            f"/teleconsult/clinics/{w.clinic}/",
            {"action": "join", "session_id": str(w.session)},
        )
    assert summary.status_code == 200
    document = Document(summary.content)
    assert any(
        "data-patient-devices" in attributes for _, attributes in document.elements
    )
    assert {
        attributes["data-summary"]: attributes["data-summary-state"]
        for attributes in document.tagged("li")
        if "data-summary" in attributes
    } == results


def test_native_device_form_keeps_its_fields_and_patient_authority(
    rbac_graph: RbacGraph, synthetic_provider: object
) -> None:
    w = seed_world(rbac_graph)
    patient = _patient_client(w.patient_session)
    results = {
        "camera": "error",
        "microphone": "ok",
        "speaker": "not_checked",
        "network": "offline",
    }
    with runtime_role():
        page = patient.get(PATIENT_PATH)
        assert page.status_code == 200
        document = Document(page.content)
        assert {field["name"] for field in document.tagged("select")} == set(results)
        document.assert_every_form_is_post_with_csrf()
        response = patient.post(
            PATIENT_PATH,
            {"action": "device_check", "session_id": str(w.session), **results},
        )
        assert response.status_code == 200
        assert any(
            "data-device-saved" in attributes
            for _, attributes in Document(response.content).elements
        )
        refused = patient.post(
            PATIENT_PATH,
            {"action": "device_check", "session_id": str(uuid4()), **results},
        )
        assert refused.status_code == 403
    with runtime_role(), tenant_context(w.graph.physician, w.organization):
        check = TeleconsultDeviceCheck.objects.get(session_id=w.session)
        assert check.role == "patient"
        assert {field: getattr(check, field) for field in results} == results
