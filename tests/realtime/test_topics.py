"""Full grammar through ticket/stream views and real permission/Redis boundaries."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from apps.identity.current_context import CurrentActorError
from apps.identity.models import CareTeamMembership, User
from apps.intake.models import PATIENT_OPERATION_VALUES, PatientSession
from apps.realtime.authorization import TopicDeniedError, authorize_topics_sync
from apps.realtime.scopes import (
    grant_clinic_topic,
    register_job_topic,
    revoke_clinic_topic,
)
from apps.realtime.transport import publish_on_commit, redis_client, topic_hash
from apps.realtime.views import stream_view, ticket_view
from apps.tenancy.db import tenant_context
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.db import transaction
from django.http import JsonResponse, StreamingHttpResponse
from django.test import RequestFactory
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY

from identity.permission_support import owner_context, permission_actor
from otp_test_support import create_totp_device
from patient_service_support import runtime_role
from realtime.test_authorization import session_key
from realtime.test_stream import runtime_connection
from renewal.test_self_booking import _client, _session
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("real_redis"),
]


def actor_session(graph: RbacGraph, actor: UUID) -> str:
    user = User.objects.get(pk=actor)
    device = create_totp_device(actor, confirmed=True)
    session = SessionStore()
    session[SESSION_KEY] = str(actor)
    session[BACKEND_SESSION_KEY] = "apps.identity.auth_backends.ClinicBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session["active_org_id"] = str(graph.organization_a)
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    assert session.session_key is not None
    return session.session_key


def roundtrip(key: str, topic: str, kind: str) -> None:
    factory = RequestFactory()
    request = factory.post(
        "/rt/stream", {"topics": [topic]}, content_type="application/json"
    )
    request.COOKIES["sessionid"] = key
    with runtime_connection():
        ticket = ticket_view(request)
        assert isinstance(ticket, JsonResponse)
        assert ticket.status_code == 200
        opened = factory.get("/rt/stream", {"t": json.loads(ticket.content)["ticket"]})
        opened.COOKIES["sessionid"] = key

        async def exercise() -> None:
            response = await stream_view(opened)
            assert isinstance(response, StreamingHttpResponse)
            assert response.status_code == 200
            events = cast("AsyncGenerator[bytes]", response.streaming_content)
            try:
                async with asyncio.timeout(5):
                    assert (await anext(events)).startswith(b"event: ready")
                    await sync_to_async(publish_on_commit)(
                        topic=topic, kind=kind, version=71
                    )
                    frame = await anext(events)
                    assert json.loads(frame.removeprefix(b"data: ")) == {
                        "topic_hash": topic_hash(topic),
                        "kind": kind,
                        "version": 71,
                    }
                    assert topic.encode() not in frame
                    await sync_to_async(publish_on_commit)(
                        topic="authz:halt", kind="halted", version=1
                    )
                    assert (await anext(events)).startswith(b"event: closed")
                    with pytest.raises(StopAsyncIteration):
                        await anext(events)
            finally:
                await events.aclose()
                await sync_to_async(response.close)()

        async_to_sync(exercise)()


@pytest.mark.parametrize("kind", ["inbox", "messages"])
def test_clinic_topics_need_explicit_scoped_lease_and_current_permission(
    rbac_graph: RbacGraph,
    kind: str,
) -> None:
    key = session_key(rbac_graph)
    topic = f"clinic:{rbac_graph.clinic_a}:{kind}"
    operator, _ = permission_actor(rbac_graph, "org_admin")
    permission = "appointment.read" if kind == "inbox" else "charge.read"
    with runtime_role(), pytest.raises(TopicDeniedError):
        authorize_topics_sync(session_key=key, topics=(topic,))
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        pytest.raises(CurrentActorError),
    ):
        grant_clinic_topic(
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.shared_user,
            kind=kind,
            permission=permission,
        )
    with runtime_role(), tenant_context(operator, rbac_graph.organization_a):
        assert (
            grant_clinic_topic(
                clinic_id=rbac_graph.clinic_a,
                user_id=rbac_graph.shared_user,
                kind=kind,
                permission=permission,
            )
            == topic
        )
    roundtrip(key, topic, kind)
    with runtime_role(), tenant_context(operator, rbac_graph.organization_a):
        revoke_clinic_topic(
            clinic_id=rbac_graph.clinic_a, user_id=rbac_graph.shared_user, kind=kind
        )
    with runtime_role(), pytest.raises(TopicDeniedError):
        authorize_topics_sync(session_key=key, topics=(topic,))


def test_job_topic_binds_owner_and_originating_permission(
    rbac_graph: RbacGraph,
) -> None:
    key = session_key(rbac_graph)
    other, _ = permission_actor(rbac_graph, "receptionist")
    other_key = actor_session(rbac_graph, other)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        topic = register_job_topic(
            clinic_id=rbac_graph.clinic_a, permission="appointment.read"
        )
    roundtrip(key, topic, "ai_job")
    with runtime_role(), pytest.raises(TopicDeniedError):
        authorize_topics_sync(session_key=other_key, topics=(topic,))
    forged_topic = "ai_job:" + uuid4().hex
    with redis_client() as broker:
        binding = broker.get("rt-scope:" + topic_hash(topic))
        assert isinstance(binding, bytes)
        broker.set("rt-scope:" + topic_hash(forged_topic), binding)
    with runtime_role(), pytest.raises(TopicDeniedError):
        authorize_topics_sync(session_key=key, topics=(forged_topic,))


def test_clinical_job_keeps_patient_scope_and_care_team_authority(
    rbac_graph: RbacGraph,
) -> None:
    actor, enrollment = permission_actor(rbac_graph, "physician")
    key = actor_session(rbac_graph, actor)
    with runtime_role(), tenant_context(actor, rbac_graph.organization_a):
        topic = register_job_topic(
            clinic_id=rbac_graph.clinic_a,
            permission="clinical.read",
            patient_enrollment_id=enrollment,
        )
        with pytest.raises(CurrentActorError):
            register_job_topic(
                clinic_id=rbac_graph.clinic_a,
                permission="clinical.read",
                patient_enrollment_id=uuid4(),
            )
    roundtrip(key, topic, "ai_job")
    with owner_context(rbac_graph.organization_a):
        CareTeamMembership.objects.filter(user_id=actor).update(
            revoked_at=timezone.now()
        )
    with runtime_role(), pytest.raises(TopicDeniedError):
        authorize_topics_sync(session_key=key, topics=(topic,))


@pytest.mark.parametrize("operation", PATIENT_OPERATION_VALUES)
def test_patient_topics_bind_exact_enrollment_and_operation(
    rbac_graph: RbacGraph, operation: str
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    patient_id = _session(setup)
    client = _client(patient_id)
    key = client.session.session_key
    assert key is not None
    topic = f"patient:{setup.enrollment_id}:{operation}"
    roundtrip(key, topic, "agenda" if operation == "booking" else "inbox")
    with runtime_role(), pytest.raises(TopicDeniedError):
        authorize_topics_sync(
            session_key=key, topics=(f"patient:{uuid4()}:{operation}",)
        )
    with owner_context(setup.organization_id):
        PatientSession.objects.filter(pk=patient_id).update(
            operations=["billing" if operation != "billing" else "booking"]
        )
    with runtime_role(), pytest.raises(TopicDeniedError):
        authorize_topics_sync(session_key=key, topics=(topic,))


def test_booking_publishes_queue_and_exact_patient_topic(rbac_graph: RbacGraph) -> None:
    setup = seed_appointment_setup(rbac_graph)
    topics = {
        f"clinic:{setup.clinic_id}:queue": "queue",
        f"patient:{setup.enrollment_id}:booking": "agenda",
    }
    with redis_client() as broker, broker.pubsub() as listener:
        listener.subscribe(*("rt:" + topic_hash(topic) for topic in topics))
        for _ in topics:
            acknowledgment = listener.get_message(timeout=3)
            assert acknowledgment is not None
            assert acknowledgment["type"] == "subscribe"
        with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
            create_synthetic_appointment(setup)
        received = []
        for _ in topics:
            message = listener.get_message(timeout=3)
            assert message is not None
            assert isinstance(message["data"], bytes)
            received.append(json.loads(message["data"]))
        assert received == [
            {"topic_hash": topic_hash(topic), "kind": kind, "version": 1}
            for topic, kind in topics.items()
        ]
    roundtrip(session_key(rbac_graph), f"clinic:{setup.clinic_id}:queue", "queue")


def test_scope_registration_rollback_never_creates_authority(
    rbac_graph: RbacGraph,
) -> None:
    key = session_key(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        topic = register_job_topic(
            clinic_id=rbac_graph.clinic_a, permission="appointment.read"
        )
        with redis_client() as broker:
            assert broker.get("rt-scope:" + topic_hash(topic)) is None
        transaction.set_rollback(True)
    with runtime_role(), pytest.raises(TopicDeniedError):
        authorize_topics_sync(session_key=key, topics=(topic,))
