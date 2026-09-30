"""Patient and video refusal exits, using issued patient and staff identities."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest
from apps.intake import views as intake_views
from apps.intake.patient_access import PATIENT_SESSION_KEY
from apps.prescription import views as prescription_views
from apps.prescription.verification import VerificationLimitedError
from apps.retention import views as retention_views
from apps.retention.services import RetentionConflictError
from apps.teleconsult import views as video_views
from apps.teleconsult.services import TeleconsultConflictError
from django.test import Client

from otp_test_support import runtime_role
from renewal.test_encounters import physician_client
from renewal.test_teleconsult_sessions import _create, seed, synthetic_provider

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

__all__ = ["synthetic_provider"]
pytestmark = pytest.mark.django_db(transaction=True)


def test_video_conflicts_and_patient_refusals(
    rbac_graph: RbacGraph, synthetic_provider: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    _, encounter, _, patient_id, _ = seed(graph)
    video = _create(graph, encounter)
    staff_url = f"/teleconsult/clinics/{graph.clinic_a}/"
    # Starting while the room is still pending is a genuine lifecycle conflict.
    with physician_client(graph) as staff:
        for fields, headers in (
            ({"action": "start"}, {}),
            ({"action": "start", "surface": "workspace"}, {}),
            ({"action": "start", "surface": "workspace"}, {"HX-Request": "true"}),
        ):
            response = staff.post(
                staff_url, {**fields, "session_id": str(video.pk)}, headers=headers
            )
            assert response.status_code == 409
    patient = Client()
    session = patient.session
    session[PATIENT_SESSION_KEY] = str(patient_id)
    session.save()
    with runtime_role():
        assert (
            patient.post(
                "/patient/teleconsult/",
                {"action": "invalid", "session_id": str(video.pk)},
            ).status_code
            == 403
        )
        assert (
            patient.post(
                "/patient/teleconsult/",
                {"action": "status", "session_id": str(uuid4())},
            ).status_code
            == 403
        )
        assert (
            patient.post(
                "/patient/teleconsult/", {"action": "join", "session_id": str(video.pk)}
            ).status_code
            == 409
        )
        assert (
            patient.post("/patient/documents/", {"action": "invalid"}).status_code
            == 403
        )
        assert (
            patient.post(
                "/patient/documents/",
                {"action": "download", "document_id": str(uuid4())},
            ).status_code
            == 403
        )
        # Recheck can lose authority after the middleware's initial touch.
        for module, url in (
            (intake_views, "/patient/"),
            (prescription_views, "/patient/documents/"),
            (retention_views, "/patient/records/"),
            (video_views, "/patient/teleconsult/"),
        ):
            with monkeypatch.context() as patch:
                patch.setattr(
                    module, "patient_session_overview", Mock(return_value=None)
                )
                assert patient.get(url).status_code == 403
        with monkeypatch.context() as patch:
            patch.setattr(
                retention_views,
                "patient_released_records",
                Mock(side_effect=RetentionConflictError("precondition_failed")),
            )
            assert patient.get("/patient/records/").status_code == 403
        with monkeypatch.context() as patch:
            patch.setattr(
                video_views,
                "request_patient_join",
                Mock(side_effect=TeleconsultConflictError("precondition_failed")),
            )
            assert (
                patient.post(
                    "/patient/teleconsult/",
                    {"action": "join", "session_id": str(video.pk)},
                ).status_code
                == 409
            )
        with monkeypatch.context() as patch:
            patch.setattr(
                prescription_views,
                "verify_handle",
                Mock(side_effect=VerificationLimitedError()),
            )
            assert patient.get("/prescription/verify/synthetic/").status_code == 429
        with monkeypatch.context() as patch:
            patch.setattr(
                prescription_views,
                "receive_signature_callback",
                Mock(return_value="rejected"),
            )
            assert (
                patient.post(
                    "/prescription/signing/callback/synthetic/", {}
                ).status_code
                == 403
            )
