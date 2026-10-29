"""Document selector refusals preserve all rows, audit and session state."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.prescription.models import DocumentKind
from apps.prescription.policy import SYNTHETIC_CATEGORY
from apps.prescription.services import create_draft
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.urls import reverse

from ehr.test_refusal_matrix import audit_after, snapshot
from patient_service_support import runtime_role
from prescription.test_document_kinds import rendered, saved, scoped
from renewal.test_encounters import physician_client

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_clinical_workspace_refuses_legacy_prescription_without_effects(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
) -> None:
    # Given: a real legacy prescription belongs to this authorized physician.
    scope = scoped(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        legacy = create_draft(**scope, category=SYNTHETIC_CATEGORY)
    url = reverse("prescription:clinical-documents", args=(rbac_graph.clinic_a,))
    with physician_client(rbac_graph) as client:
        assert client.get(url).status_code == 200
        before, tail = snapshot(superuser_database_url)
        session = dict(client.session)
        # When: the non-prescription workspace receives the legacy selector.
        response = client.post(url, {"action": "open_draft", "draft_id": legacy.pk})
        # Then: refusal cannot select a draft, write audit or change the session.
        assert response.status_code == 403
        assert snapshot(superuser_database_url) == (before, tail)
        assert audit_after(superuser_database_url, tail) == []
        assert dict(client.session) == session
        assert settings.SESSION_COOKIE_NAME not in response.cookies


@pytest.mark.parametrize(
    "action",
    ["release_document", "revoke_release", "deliver_document", "revoke_document"],
)
def test_clinical_workspace_refuses_another_drafts_document_without_effects(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    action: str,
) -> None:
    # Given: two real document kinds have distinct drafts and frozen artifacts.
    scope = scoped(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        selected = saved(rbac_graph, scope)
        referral = saved(rbac_graph, scope, DocumentKind.REFERRAL)
        other_document = rendered(rbac_graph, scope, referral)
    url = reverse("prescription:clinical-documents", args=(rbac_graph.clinic_a,))
    with physician_client(rbac_graph) as client:
        assert client.get(url).status_code == 200
        before, tail = snapshot(superuser_database_url)
        session = dict(client.session)
        # When: a lifecycle command mismatches the selected draft and artifact.
        response = client.post(
            url,
            {
                "action": action,
                "draft_id": selected.pk,
                "document_id": other_document.pk,
                "expected_version": selected.version,
            },
        )
        # Then: no lifecycle effect or patient-context persistence is authorized.
        assert response.status_code == 403
        assert snapshot(superuser_database_url) == (before, tail)
        assert audit_after(superuser_database_url, tail) == []
        assert dict(client.session) == session
        assert settings.SESSION_COOKIE_NAME not in response.cookies
