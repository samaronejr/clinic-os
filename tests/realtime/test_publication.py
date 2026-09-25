"""Commit, rollback, logout and role-revocation invalidation receipts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.realtime import transport
from apps.realtime.transport import publish_on_commit, redis_client, topic_hash
from django.contrib.auth import logout
from django.db import connection, transaction
from django.http import HttpRequest
from django.test import Client

from patient_service_support import runtime_role
from realtime.test_authorization import session_key

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("real_redis"),
]


def test_publication_is_after_commit_and_rollback_discards_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic = f"clinic:{uuid4()}:agenda"
    called = []
    real_publish = transport.publish

    def publish(topic: str, kind: str, version: int) -> None:
        called.append(connection.in_atomic_block)
        real_publish(topic=topic, kind=kind, version=version)

    monkeypatch.setattr(transport, "publish", publish)
    with redis_client() as client, client.pubsub() as listener:
        listener.subscribe("rt:" + topic_hash(topic))
        acknowledgment = listener.get_message(timeout=3)
        assert acknowledgment is not None
        assert acknowledgment["type"] == "subscribe"
        with transaction.atomic():
            publish_on_commit(topic=topic, kind="agenda", version=1)
            assert called == []
            transaction.set_rollback(True)
        assert called == []
        with transaction.atomic():
            publish_on_commit(topic=topic, kind="agenda", version=2)
            assert called == []
        assert called == [False]
        message = listener.get_message(timeout=3)
        assert message is not None
        assert isinstance(message["data"], bytes)
        assert json.loads(message["data"])["version"] == 2


def test_role_revocation_emits_only_after_commit(rbac_graph: RbacGraph) -> None:
    topic = f"authz:user:{rbac_graph.shared_user}"
    with redis_client() as client, client.pubsub() as listener:
        listener.subscribe("rt:" + topic_hash(topic))
        acknowledgment = listener.get_message(timeout=3)
        assert acknowledgment is not None
        assert acknowledgment["type"] == "subscribe"
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [str(rbac_graph.organization_a)],
            )
            UserClinicRole.objects.filter(
                user_id=rbac_graph.shared_user, clinic_id=rbac_graph.clinic_a
            ).delete()
        message = listener.get_message(timeout=3)
        assert message is not None
        assert isinstance(message["data"], bytes)
        assert json.loads(message["data"]) == {
            "topic_hash": topic_hash(topic),
            "kind": "revoked",
            "version": 1,
        }


def test_logout_emits_the_same_revocation_channel(rbac_graph: RbacGraph) -> None:
    key = session_key(rbac_graph)
    topic = f"authz:user:{rbac_graph.shared_user}"
    request = HttpRequest()
    request.user = User.objects.get(pk=rbac_graph.shared_user)
    client = Client()
    client.cookies["sessionid"] = key
    request.session = client.session
    with redis_client() as redis, redis.pubsub() as listener:
        listener.subscribe("rt:" + topic_hash(topic))
        acknowledgment = listener.get_message(timeout=3)
        assert acknowledgment is not None
        assert acknowledgment["type"] == "subscribe"
        with runtime_role():
            logout(request)
        message = listener.get_message(timeout=3)
        assert message is not None
        assert isinstance(message["data"], bytes)
        assert json.loads(message["data"])["kind"] == "revoked"
