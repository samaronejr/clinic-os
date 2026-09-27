"""Late clinical refusals keep real authority, subjects and HTTP middleware."""

from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest
from apps.ehr import views as ehr_views
from apps.ehr.services import ClinicalConflictError
from apps.prescription import views as prescription_views
from apps.prescription.services import DocumentStorageError, discard_draft
from apps.prescription.signing import initiate_signature
from apps.tenancy.db import tenant_context
from django.urls import reverse

from otp_test_support import runtime_role
from renewal.test_document_artifacts import seed_rendered
from renewal.test_encounters import draft as clinical_draft
from renewal.test_encounters import physician_client, seed
from renewal.test_prescribing_workflow import _provision

if TYPE_CHECKING:
    from django.http import HttpResponseBase
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _refused(response: HttpResponseBase) -> None:
    assert response.status_code == 403
    assert not response.cookies


def test_prescription_late_draft_and_review_refusals(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    scope, document = seed_rendered(graph)
    draft = document.draft
    author = reverse(
        "prescription:draft-encounter",
        kwargs={"clinic_id": graph.clinic_a, "encounter_id": scope["encounter_id"]},
    )
    review = reverse(
        "prescription:review",
        kwargs={"clinic_id": graph.clinic_a, "document_id": document.pk},
    )
    with physician_client(graph) as client:
        _refused(
            client.post(
                author, {"action": "review_document", "document_id": str(uuid4())}
            )
        )
        _refused(client.post(review, {"action": "invalid"}))
        with monkeypatch.context() as patch:
            recheck = Mock(side_effect=ClinicalConflictError("precondition_failed"))
            patch.setattr(prescription_views, "request_signature", recheck)
            assert client.post(review, {"action": "sign_document"}).status_code == 409
            recheck.assert_called_once()
        with monkeypatch.context() as patch:
            patch.setattr(
                prescription_views,
                "download_document",
                Mock(side_effect=DocumentStorageError()),
            )
            _refused(client.post(review, {"action": "download_document"}))
            _refused(
                client.post(
                    author,
                    {"action": "download_document", "document_id": str(document.pk)},
                )
            )
        # The DB binding is immutable. Simulate an inconsistent authorized
        # service result, not an authorization bypass, to test the view's fence.
        with tenant_context(graph.physician, graph.organization_a):
            wrong = copy(document.encounter)
        wrong.physician_id = uuid4()
        with monkeypatch.context() as patch:
            patch.setattr(
                prescription_views, "authorize_encounter", Mock(return_value=wrong)
            )
            _refused(client.get(review))
        with tenant_context(graph.physician, graph.organization_a):
            discard_draft(**scope, draft_id=draft.pk, expected_version=draft.version)
        _refused(
            client.post(
                author, {"action": "render_document", "document_id": str(document.pk)}
            )
        )
        _refused(
            client.post(
                author, {"action": "sign_document", "document_id": str(document.pk)}
            )
        )
        _refused(client.post(review, {"action": "sign_document"}))


def test_prescription_signing_failure_boundaries(
    rbac_graph: RbacGraph, settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    _scope, document = seed_rendered(graph)
    settings.PRESCRIPTION_SYNTHETIC_SIGNING = True
    _provision(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        operation = initiate_signature(
            clinic_id=graph.clinic_a, document_id=document.pk
        )
    url = reverse(
        "prescription:signing",
        kwargs={"clinic_id": graph.clinic_a, "operation_id": operation.pk},
    )
    with physician_client(graph) as client:
        _refused(client.post(url, {"action": "restart_signature"}))
        _refused(client.post(url, {"action": "invalid"}))
        with monkeypatch.context() as patch:
            patch.setattr(
                prescription_views,
                "download_signed_document",
                Mock(side_effect=DocumentStorageError()),
            )
            _refused(client.post(url, {"action": "download_signed"}))
        with monkeypatch.context() as patch:
            patch.setattr(
                prescription_views,
                "signature_status",
                Mock(side_effect=ClinicalConflictError("precondition_failed")),
            )
            _refused(client.get(url))
        assert client.post(url, {"action": "abandon"}).status_code == 302
        assert client.post(url, {"action": "abandon"}).status_code == 409


def test_ehr_review_rejects_an_inconsistent_authorized_version(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = clinical_draft(graph, appointment, template)
    encounter_id = version.document.encounter_id
    other = copy(version.document)
    other.encounter_id = uuid4()
    version.document = other
    with monkeypatch.context() as patch:
        patch.setattr(ehr_views, "view_version", Mock(return_value=version))
        with physician_client(graph) as client:
            response = client.post(
                reverse("ehr:encounter", kwargs={"clinic_id": graph.clinic_a}),
                {
                    "action": "review",
                    "encounter_id": str(encounter_id),
                    "version_id": str(version.pk),
                },
            )
    _refused(response)
    # No prescription draft exists on this otherwise authorized encounter.
    with physician_client(graph) as client:
        _refused(
            client.post(
                reverse(
                    "prescription:draft-encounter",
                    kwargs={"clinic_id": graph.clinic_a, "encounter_id": encounter_id},
                ),
                {"action": "review_document", "document_id": str(uuid4())},
            )
        )
