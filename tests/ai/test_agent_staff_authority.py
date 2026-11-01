"""The agent queue follows the RP automation row; task assignment never opens it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import pytest
from apps.ai import agent_views
from apps.ai.models import ActionProposal, Approval
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import connection

from ai.agent_support import AgentWorld, propose
from ai.test_agent_execution import world
from ai.test_agent_guards import request_for
from auth.stepup_test_support import create_role_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponseBase

    from rbac_fixtures import RbacGraph

__all__ = ("world",)
# No machine login: it commits ALTER ROLE clinic_agent (pg_authid), which would make
# every test here server-state. Nothing below opens the agent connection.
pytestmark = pytest.mark.django_db(transaction=True)

OK: Final = 200
FORBIDDEN: Final = 403
# RP "Automations/agents policy": manager = admin, org admin = org. Owner and
# org admin share one bundle; clinic admin and manager share another.
ADMINISTRATORS: Final = ("owner", "org_admin", "clinic_admin", "clinic_manager")
OTHERS: Final = (
    "nurse",
    "allied_professional",
    "receptionist",
    "scheduler",
    "physician",
    "finance",
)


def _actor(graph: RbacGraph, role: str, version: int) -> UUID:
    user = create_role_actor(graph, UserClinicRole.Role(role))
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(
            user=user, clinic_id=graph.clinic_a, role=role
        ).update(workflow_bundle_version=version)
    return user.pk


def _agent_staff(graph: RbacGraph, actor: UUID, clinic: UUID) -> bool:
    with (
        runtime_role(),
        tenant_context(actor, graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT clinic_app.agent_staff(%s)", [clinic])
        return bool(cursor.fetchone() == (True,))


def _queue(
    graph: RbacGraph, actor: UUID, clinic: UUID, data: dict[str, str] | None = None
) -> tuple[HttpRequest, HttpResponseBase]:
    request = request_for(actor, clinic, "ai:agents", data)
    with runtime_role(), tenant_context(actor, graph.organization_a):
        return request, agent_views.agent_queue(request, clinic)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("role", [*ADMINISTRATORS, *OTHERS])
def test_agent_staff_admits_only_automation_authority(
    rbac_graph: RbacGraph, role: str, version: int
) -> None:
    actor = _actor(rbac_graph, role, version)

    assert _agent_staff(rbac_graph, actor, rbac_graph.clinic_a) is (
        role in ADMINISTRATORS
    )
    assert _agent_staff(rbac_graph, actor, uuid4()) is False


@pytest.mark.parametrize("role", OTHERS)
def test_workflow_v2_roles_cannot_open_or_reject_agent_proposals(
    world: AgentWorld, role: str
) -> None:
    # Given a validated proposal and a v2 role whose bundle holds tasks.assign.
    graph = world.gateway.graph
    proposal = propose(world)
    actor = _actor(graph, role, 2)
    reject = {
        "proposal_id": str(proposal.pk),
        "digest": proposal.digest,
        "command": "reject",
    }

    # When it opens the queue or rejects, in its clinic and in an unknown one.
    for data in (None, reject):
        known_request, known = _queue(graph, actor, graph.clinic_a, data)
        unknown_request, unknown = _queue(graph, actor, uuid4(), data)

        # Then both are the same refusal and nothing in the session changed.
        assert known.status_code == unknown.status_code == FORBIDDEN
        assert not known.cookies
        assert not unknown.cookies
        assert known_request.session.modified is False
        assert unknown_request.session.modified is False

    with (
        runtime_role(),
        tenant_context(world.gateway.administrator, graph.organization_a),
    ):
        assert ActionProposal.objects.get(pk=proposal.pk).state == "validated"
        assert not Approval.objects.filter(proposal=proposal).exists()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("role", ADMINISTRATORS)
def test_administrators_and_managers_keep_the_agent_queue(
    rbac_graph: RbacGraph, role: str, version: int
) -> None:
    actor = _actor(rbac_graph, role, version)

    _request, response = _queue(rbac_graph, actor, rbac_graph.clinic_a)

    assert response.status_code == OK
