"""Approval and execution remain separate real transactions in the S09 proof."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.models import ActionExecution, ActionProposal, Approval
from apps.identity.models import User, UserClinicRole
from apps.tenancy.db import tenant_context
from django.test import Client
from django.utils import timezone

from identity.lifecycle_test_support import session_key
from identity.permission_support import owner_context
from otp_test_support import create_totp_device, token_for
from patient_service_support import runtime_role
from renewal.agent_proposal_fixture import seed
from renewal.flagship_fixture import _approve_without_dispatch, _execute_approved

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("revoke", [False, True])
def test_executor_rechecks_two_committed_browser_session_approvals(
    rbac_graph: RbacGraph,
    synthetic_secret_backend: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    revoke: bool,
) -> None:
    graph = rbac_graph
    monkeypatch.setenv("CLINIC_SECRET_DIR", str(synthetic_secret_backend))
    first, second = graph.clinic_admin, graph.shared_user
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=first,
            role="clinic_admin",
        )
        for actor in (first, second):
            for clinic in (graph.clinic_a, graph.clinic_b):
                UserClinicRole.objects.get_or_create(
                    organization_id=graph.organization_a,
                    clinic_id=clinic,
                    user_id=actor,
                    role="org_admin",
                )
    proposal, _revision = seed(first, graph.clinic_a, graph.organization_a, second)
    with runtime_role(), tenant_context(first, graph.organization_a):
        digest = ActionProposal.objects.get(pk=proposal).digest
    scope = {
        "organization": str(graph.organization_a),
        "clinic": str(graph.clinic_a),
        "proposal": str(proposal),
    }
    for actor, expected in ((first, "validated"), (second, "approved")):
        user = User.objects.get(pk=actor)
        device = create_totp_device(actor, confirmed=True)
        client = Client()
        with runtime_role():
            client.force_login(user)
            verified = client.post(
                "/auth/verify/",
                {
                    "otp_device": device.persistent_id,
                    "otp_token": token_for(device),
                    "next": "/auth/protected/",
                },
            )
        assert verified.status_code == 302
        receipt = _approve_without_dispatch(
            {
                **scope,
                "actor": str(actor),
                "digest": digest,
                "session": session_key(client),
            }
        )
        assert receipt == {"state": expected}
    with runtime_role(), tenant_context(second, graph.organization_a):
        assert Approval.objects.filter(proposal_id=proposal).count() == 2
        assert ActionProposal.objects.get(pk=proposal).state == "approved"
        assert not ActionExecution.objects.filter(proposal_id=proposal).exists()
    if revoke:
        with owner_context(graph.organization_a):
            UserClinicRole.objects.filter(
                user_id=first, clinic_id=graph.clinic_a, revoked_at__isnull=True
            ).update(revoked_at=timezone.now())
    result = _execute_approved({**scope, "actor": str(second)})
    assert result == {
        "state": "rejected" if revoke else "receipt",
        "reason": "authority_revoked" if revoke else None,
        "execution_created": not revoke,
        "receipt_created": not revoke,
    }
    with runtime_role(), tenant_context(second, graph.organization_a):
        assert ActionExecution.objects.filter(proposal_id=proposal).count() == (
            0 if revoke else 1
        )
