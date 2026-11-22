"""Commit, rollback, logout and role-revocation invalidation receipts."""

from __future__ import annotations

import json
from os import getpid
from select import select
from socket import socket
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
from django.utils import timezone
from redis import Redis
from redis.connection import Connection
from redis.exceptions import ConnectionError as RedisConnectionError

from patient_service_support import runtime_role
from realtime.test_authorization import session_key

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_django.fixtures import SettingsWrapper
    from redis.client import PubSub

    from rbac_fixtures import RbacGraph


pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("real_redis"),
]


def _expect_version(listener: PubSub, version: int) -> None:
    message = listener.get_message(timeout=3)
    assert message is not None
    assert message["type"] == "message"
    assert isinstance(message["data"], bytes)
    assert json.loads(message["data"])["version"] == version


def test_committed_hints_reuse_one_socket_and_reconnect_after_broker_disconnect(
    monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    topic = f"clinic:{uuid4()}:inbox"
    connect: Callable[[Connection], object] = Connection._connect
    opened: list[socket] = []

    def track_connect(peer: Connection) -> socket:
        sock = connect(peer)
        assert isinstance(sock, socket)
        opened.append(sock)
        return sock

    monkeypatch.setattr(Connection, "_connect", track_connect)
    # Make the cold connection count independent of other tests' publications.
    transport._publisher_pool.cache_clear()
    with redis_client() as client, client.pubsub() as listener:
        listener.subscribe("rt:" + topic_hash(topic))
        acknowledgment = listener.get_message(timeout=3)
        assert acknowledgment is not None
        assert acknowledgment["type"] == "subscribe"
        assert client.ping()
        before = len(opened)
        for version in (1, 2):
            with transaction.atomic():
                publish_on_commit(topic=topic, kind="inbox", version=version)
            _expect_version(listener, version)

        assert len(opened) == before + 1
        idle_socket = opened[-1]
        pool = transport._publisher_pool(settings.REALTIME_REDIS_URL, getpid())
        with Redis(connection_pool=pool) as publisher:
            publisher_id = publisher.client_id()
        # Close the warm socket from the broker side, not via a client reset.
        assert client.client_kill_filter(_id=publisher_id) == 1
        assert select([idle_socket], [], [], 3)[0] == [idle_socket]
        with transaction.atomic():
            publish_on_commit(topic=topic, kind="inbox", version=3)
        _expect_version(listener, 3)
        assert len(opened) == before + 2


def test_lost_publish_reply_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    topic = f"clinic:{uuid4()}:inbox"
    parse_response: Callable[..., object] = Redis.parse_response
    replies = 0

    def lose_reply(
        client: Redis, connection: Connection, command_name: str, **options: object
    ) -> object:
        nonlocal replies
        result = parse_response(client, connection, command_name, **options)
        if command_name == "PUBLISH":
            replies += 1
            if replies == 1:
                # The broker sent the frame, but the caller never saw the ack.
                raise RedisConnectionError
        return result

    monkeypatch.setattr(Redis, "parse_response", lose_reply)
    with redis_client() as client, client.pubsub() as listener:
        listener.subscribe("rt:" + topic_hash(topic))
        acknowledgment = listener.get_message(timeout=3)
        assert acknowledgment is not None
        assert acknowledgment["type"] == "subscribe"
        for version in (1, 2):
            with transaction.atomic():
                publish_on_commit(topic=topic, kind="inbox", version=version)
            _expect_version(listener, version)
    assert replies == 2
    assert caplog.messages == ["realtime publication unavailable; polling required"]


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
            membership = UserClinicRole.objects.get(
                user_id=rbac_graph.shared_user, clinic_id=rbac_graph.clinic_a
            )
            membership.revoked_at = timezone.now()
            membership.save(update_fields=("revoked_at",))
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
