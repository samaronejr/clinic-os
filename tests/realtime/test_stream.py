"""Real Redis + clinic_app stream proofs, with deterministic reauth deadlines."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.realtime import stream as stream_module
from apps.realtime.authorization import authorize_topics_sync
from apps.realtime.stream import EventStream, _event
from apps.realtime.tickets import issue_ticket
from apps.realtime.transport import event_bytes, publish, redis_client, topic_hash
from apps.realtime.views import stream_view
from asgiref.sync import async_to_sync
from django.db import connection, transaction
from django.test import RequestFactory
from psycopg.conninfo import conninfo_to_dict

from database_urls import database_url_for_name
from realtime.test_authorization import session_key

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("real_redis"),
]


@contextmanager
def runtime_connection() -> Iterator[str]:
    original = deepcopy(connection.settings_dict)
    connection.close()
    name = "realtime-test-" + uuid4().hex
    connection.settings_dict["USER"] = "clinic_app"
    connection.settings_dict["PASSWORD"] = conninfo_to_dict(
        os.environ["APP_DATABASE_URL"]
    )["password"]
    connection.settings_dict["OPTIONS"]["application_name"] = name
    try:
        yield name
    finally:
        connection.close()
        connection.settings_dict.clear()
        connection.settings_dict.update(original)


def _revoke(graph: RbacGraph) -> None:
    dsn = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )
    with psycopg.connect(dsn) as owner:
        owner.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(graph.organization_a)],
        )
        owner.execute(
            "DELETE FROM clinic_app.identity_userclinicrole "
            "WHERE user_id = %s AND clinic_id = %s",
            [graph.shared_user, graph.clinic_a],
        )


def test_stream_has_no_database_backend_while_idle_and_filters_payloads(
    rbac_graph: RbacGraph,
) -> None:
    key = session_key(rbac_graph)
    topic = f"clinic:{rbac_graph.clinic_a}:agenda"
    super_dsn = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )
    with runtime_connection() as application_name:
        subscription = authorize_topics_sync(key, (topic,))

        async def exercise() -> None:
            stream = EventStream(subscription)
            events = stream.events()
            try:
                assert (await anext(events)).startswith(b"event: ready\n")
                # Probe PostgreSQL, not just Django's in_atomic_block flag. The
                # stream's actual clinic_app backend has been disconnected.
                with psycopg.connect(super_dsn) as probe:
                    assert (
                        probe.execute(
                            "SELECT pid, state, xact_start FROM pg_stat_activity "
                            "WHERE application_name = %s",
                            [application_name],
                        ).fetchall()
                        == []
                    )
                await stream.client.publish(
                    "rt:" + topic_hash(topic), b'{"text":"SINTETICO-SENTINELA-PHI"}'
                )
                await stream.client.publish(
                    "rt:" + topic_hash(topic), event_bytes(topic, "agenda", 3)
                )
                async with asyncio.timeout(3):
                    frame = await anext(events)
                assert set(json.loads(frame.removeprefix(b"data: "))) == {
                    "topic_hash",
                    "kind",
                    "version",
                }
                assert b"SINTETICO" not in frame
                assert str(rbac_graph.clinic_a).encode() not in frame
            finally:
                await events.aclose()
            assert stream.pubsub.connection is None

        async_to_sync(exercise)()


@pytest.mark.parametrize("action", ["revocation", "halt", "reauthorize", "cap"])
def test_stream_closes_on_revocation_or_deadline(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    key = session_key(rbac_graph)
    topic = f"clinic:{rbac_graph.clinic_a}:agenda"
    clock = [0.0]
    monkeypatch.setattr(stream_module, "monotonic", lambda: clock[0])
    with runtime_connection():
        subscription = authorize_topics_sync(key, (topic,))

        async def exercise() -> None:
            stream = EventStream(subscription)
            events = stream.events()
            assert (await anext(events)).startswith(b"event: ready\n")
            if action == "reauthorize":
                _revoke(rbac_graph)
                clock[0] = 60
            elif action == "cap":
                clock[0] = 600
            else:
                control = (
                    "authz:halt"
                    if action == "halt"
                    else f"authz:user:{rbac_graph.shared_user}"
                )
                await stream.client.publish(
                    "rt:" + topic_hash(control), event_bytes(control, "revoked", 1)
                )
            async with asyncio.timeout(3):
                frame = await anext(events)
                assert frame.startswith(b"event: closed\n")
                payload = json.loads(frame.split(b"data: ", 1)[1])
                assert payload["kind"] == ("expired" if action == "cap" else "revoked")
                with pytest.raises(StopAsyncIteration):
                    await anext(events)
            assert stream.pubsub.connection is None

        async_to_sync(exercise)()


@pytest.mark.parametrize(
    "payload", [b"null", b"[]", b"{}", b"not json", b"\xff", b"x" * 257]
)
def test_bad_broker_messages_never_reach_the_browser(payload: bytes) -> None:
    assert _event(payload, frozenset()) is None


def test_idle_receive_cannot_outlive_the_absolute_connection_cap(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = session_key(rbac_graph)
    topic = f"clinic:{rbac_graph.clinic_a}:agenda"
    clock = [0.0]
    monkeypatch.setattr(stream_module, "monotonic", lambda: clock[0])
    with runtime_connection():
        subscription = authorize_topics_sync(key, (topic,))

        async def exercise() -> None:
            stream = EventStream(subscription)
            events = stream.events()
            assert (await anext(events)).startswith(b"event: ready\n")
            clock[0] = 599
            timeouts = []

            async def receive(**options: object) -> dict[str, bytes]:
                timeouts.append(options["timeout"])
                return {
                    "channel": ("rt:" + topic_hash(stream.revocation)).encode(),
                    "data": b"",
                }

            monkeypatch.setattr(stream.pubsub, "get_message", receive)
            try:
                assert (await anext(events)).startswith(b"event: closed\n")
                assert timeouts == [1]
            finally:
                await events.aclose()

        async_to_sync(exercise)()


def test_disconnecting_before_body_iteration_opens_no_redis_subscription(
    rbac_graph: RbacGraph,
) -> None:
    key = session_key(rbac_graph)
    topic = f"clinic:{rbac_graph.clinic_a}:agenda"
    ticket = issue_ticket(key, (topic,))
    request = RequestFactory().get("/rt/stream", {"t": ticket})
    request.COOKIES["sessionid"] = key
    with runtime_connection():
        response = async_to_sync(stream_view)(request)
        assert response.status_code == 200
        with redis_client() as client:
            counts = client.pubsub_numsub("rt:" + topic_hash(topic))
            assert counts == [(("rt:" + topic_hash(topic)).encode(), 0)]
        response.close()


def test_publish_cannot_run_inside_uncommitted_transaction() -> None:
    with (
        transaction.atomic(),
        pytest.raises(RuntimeError, match="committed transaction"),
    ):
        publish(f"clinic:{uuid4()}:agenda", "agenda", 1)
