"""Every catalog role is refused wherever its bundle lacks the realtime permission.

The census may label a realtime guard "delegated"; this module does not rely on
that label. Roles come from the stored role catalog and the permitted set from
the versioned bundles, so a new role or a changed bundle changes the matrix.
Each boundary is executed as clinic_app: ticket and stream views, the
subscription check, lease authorization, lease grant/revoke and job binding.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest
from apps.identity.current_context import CurrentActorError
from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.realtime.authorization import TopicDeniedError, authorize_topics_sync
from apps.realtime.scopes import (
    grant_clinic_topic,
    register_job_topic,
    revoke_clinic_topic,
)
from apps.realtime.tickets import issue_ticket
from apps.realtime.topics import TOPIC_PERMISSIONS
from apps.realtime.views import stream_view, ticket_view
from apps.tenancy.db import tenant_context
from asgiref.sync import async_to_sync
from django.http import HttpResponseBase, JsonResponse, StreamingHttpResponse
from django.test import RequestFactory

from identity.permission_support import permission_actor
from patient_service_support import runtime_role
from realtime.test_stream import runtime_connection
from realtime.test_topics import actor_session

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from uuid import UUID

    from django.http import HttpRequest

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("real_redis"),
]

ROLES = tuple(UserClinicRole.Role.values)
LEASE_ADMIN = "staff.organization"
# The originating permission a producer binds into a lease or job topic.
LEASE_PERMISSION = "appointment.read"


def permitted(permission: str) -> frozenset[str]:
    return frozenset(
        role for role, bundle in BUNDLES_V1.items() if permission in bundle
    )


def _allowed(call: Callable[[], object], denial: type[Exception]) -> bool:
    try:
        call()
    except denial:
        return False
    return True


def test_role_catalog_is_the_bundle_catalog_and_splits_every_permission() -> None:
    assert set(BUNDLES_V1) == set(ROLES)
    for permission in {*TOPIC_PERMISSIONS.values(), LEASE_ADMIN, LEASE_PERMISSION}:
        # Both sides non-empty: the matrix proves grants and refusals.
        assert permitted(permission), permission
        assert set(ROLES) - permitted(permission), permission


def _ticket(key: str, topic: str) -> JsonResponse:
    request = RequestFactory().post(
        "/rt/stream", {"topics": [topic]}, content_type="application/json"
    )
    request.COOKIES["sessionid"] = key
    with runtime_connection():
        response = ticket_view(request)
    assert isinstance(response, JsonResponse)
    return response


async def _open_stream(request: HttpRequest) -> HttpResponseBase:
    response = await stream_view(request)
    if isinstance(response, StreamingHttpResponse):
        # Sockets are allocated only once iteration starts; close unopened.
        await cast("AsyncGenerator[bytes]", response.streaming_content).aclose()
    return response


def _stream(key: str, topic: str) -> HttpResponseBase:
    # The ticket is minted without authorization so the stream's own check is
    # the only thing between a refused role and an open subscription.
    request = RequestFactory().get(
        "/rt/stream", {"t": issue_ticket(session_key=key, topics=(topic,))}
    )
    request.COOKIES["sessionid"] = key
    with runtime_connection():
        return async_to_sync(_open_stream)(request)


def _subscribe(key: str, topic: str) -> bool:
    with runtime_role():
        return _allowed(
            lambda: authorize_topics_sync(session_key=key, topics=(topic,)),
            TopicDeniedError,
        )


def _as(graph: RbacGraph, actor: UUID, call: Callable[[], object]) -> bool:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        return _allowed(call, CurrentActorError)


@pytest.mark.parametrize("role", ROLES)
def test_ticket_and_stream_follow_the_role_bundle(
    rbac_graph: RbacGraph, role: str
) -> None:
    actor, _ = permission_actor(rbac_graph, role)
    key = actor_session(rbac_graph, actor)
    unknown = _ticket(key, "clinic:00000000-0000-4000-8000-000000000000:agenda")
    assert unknown.status_code == 403
    for kind, permission in sorted(TOPIC_PERMISSIONS.items()):
        topic = f"clinic:{rbac_graph.clinic_a}:{kind}"
        expected = role in permitted(permission)
        assert _subscribe(key, topic) is expected, (role, kind, "subscription")
        ticket = _ticket(key, topic)
        if expected:
            assert ticket.status_code == 200, (role, kind)
            assert set(json.loads(ticket.content)) == {"ticket"}
        else:
            # Refusal is byte-identical to the unknown-topic refusal.
            assert (ticket.status_code, ticket.content) == (
                unknown.status_code,
                unknown.content,
            ), (role, kind, "ticket")
        stream = _stream(key, topic)
        assert stream.status_code == (200 if expected else 403), (role, kind)
        assert isinstance(stream, StreamingHttpResponse) is expected


@pytest.mark.parametrize("role", ROLES)
def test_leases_and_jobs_follow_the_role_bundle(
    rbac_graph: RbacGraph, role: str
) -> None:
    actor, _ = permission_actor(rbac_graph, role)
    key = actor_session(rbac_graph, actor)
    operator, _ = permission_actor(rbac_graph, sorted(permitted(LEASE_ADMIN))[0])
    clinic = rbac_graph.clinic_a
    administers = role in permitted(LEASE_ADMIN)
    assert (
        _as(
            rbac_graph,
            actor,
            lambda: grant_clinic_topic(
                clinic_id=clinic,
                user_id=actor,
                kind="messages",
                permission=LEASE_PERMISSION,
            ),
        )
        is administers
    ), (role, "grant")
    assert (
        _as(
            rbac_graph,
            actor,
            lambda: revoke_clinic_topic(
                clinic_id=clinic, user_id=actor, kind="messages"
            ),
        )
        is administers
    ), (role, "revoke")
    # An administrator leases the inbox topic to this role; the subscription
    # still reruns the lease's originating permission for the recipient.
    with runtime_role(), tenant_context(operator, rbac_graph.organization_a):
        topic = grant_clinic_topic(
            clinic_id=clinic, user_id=actor, kind="inbox", permission=LEASE_PERMISSION
        )
    reads = role in permitted(LEASE_PERMISSION)
    assert _subscribe(key, topic) is reads, (role, "lease")
    jobs: list[str] = []
    assert (
        _as(
            rbac_graph,
            actor,
            lambda: jobs.append(
                register_job_topic(clinic_id=clinic, permission=LEASE_PERMISSION)
            ),
        )
        is reads
    ), (role, "job")
    for job in jobs:
        assert _subscribe(key, job), (role, "job subscription")
