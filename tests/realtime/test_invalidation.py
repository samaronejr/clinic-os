"""Commit/ACK ordered revocation proofs before the periodic backstop."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from copy import deepcopy
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import RoleGrant, UserClinicRole
from apps.realtime import stream as stream_module
from apps.realtime import transport
from apps.realtime.authorization import authorize_topics_sync
from apps.realtime.stream import EventStream
from apps.realtime.transport import event_bytes, topic_hash
from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.sessions.models import Session
from django.db import connection, transaction
from django.utils import timezone
from psycopg.conninfo import conninfo_to_dict

from database_urls import database_url_for_name
from realtime.test_authorization import session_key
from realtime.test_stream import runtime_connection

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("real_redis"),
]


def mutate(graph: RbacGraph, key: str, action: str) -> None:
    previous = deepcopy(connection.settings_dict)
    connection.close()
    owner = conninfo_to_dict(os.environ["MIGRATION_DATABASE_URL"])
    connection.settings_dict.update(USER=owner["user"], PASSWORD=owner["password"])
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [str(graph.organization_a)],
            )
            if action == "membership":
                UserClinicRole.objects.filter(
                    user_id=graph.shared_user, clinic_id=graph.clinic_a
                ).delete()
            elif action == "role":
                role = UserClinicRole.objects.get(
                    user_id=graph.shared_user, clinic_id=graph.clinic_a
                )
                role.role = "physician"
                role.save(update_fields=("role",))
            elif action == "permission":
                RoleGrant.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                    role="receptionist",
                    permission="appointment.read",
                    valid_from=timezone.now(),
                )
            elif action == "expiry":
                session = Session.objects.get(pk=key)
                session.expire_date = timezone.now() - timedelta(seconds=1)
                session.save(update_fields=("expire_date",))
            elif action == "session_delete":
                Session.objects.get(pk=key).delete()
            else:
                raise AssertionError(action)
    finally:
        connection.close()
        connection.settings_dict.clear()
        connection.settings_dict.update(previous)


@pytest.mark.parametrize(
    "action", ["membership", "role", "permission", "expiry", "session_delete"]
)
def test_committed_changes_wake_idle_stream_and_reauthorize_immediately(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    key = session_key(rbac_graph)
    topic = f"clinic:{rbac_graph.clinic_a}:agenda"
    monkeypatch.setattr(stream_module, "monotonic", lambda: 0.0)
    observed: list[bool] = []
    original = transport.publish

    def observe(*, topic: str, kind: str, version: int) -> None:
        observed.append(connection.in_atomic_block)
        original(topic=topic, kind=kind, version=version)

    monkeypatch.setattr(transport, "publish", observe)
    with runtime_connection():
        subscription = authorize_topics_sync(session_key=key, topics=(topic,))

        async def exercise() -> None:
            stream = EventStream(subscription)
            events = stream.events()
            receiver = None
            try:
                assert (await anext(events)).startswith(b"event: ready")
                receiver = asyncio.create_task(anext(events))
                await sync_to_async(mutate)(rbac_graph, key, action)
                async with asyncio.timeout(3):
                    frame = await receiver
                    assert frame.startswith(b"event: closed")
                    assert json.loads(frame.split(b"data: ")[1])["kind"] == (
                        "expired" if action == "expiry" else "revoked"
                    )
                    with pytest.raises(StopAsyncIteration):
                        await anext(events)
            finally:
                if receiver is not None and not receiver.done():
                    receiver.cancel()
                    with suppress(asyncio.CancelledError):
                        await receiver
                await events.aclose()
            assert stream.pubsub.connection is None

        async_to_sync(exercise)()
    assert observed
    assert not any(observed)


@pytest.mark.parametrize("action", ["membership", "expiry", "permission"])
def test_lost_control_message_cannot_release_a_post_commit_domain_frame(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    key = session_key(rbac_graph)
    topic = f"clinic:{rbac_graph.clinic_a}:agenda"
    monkeypatch.setattr(stream_module, "monotonic", lambda: 0.0)
    dsn = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )
    with runtime_connection():
        subscription = authorize_topics_sync(session_key=key, topics=(topic,))

        async def exercise() -> None:
            stream = EventStream(subscription)
            events = stream.events()
            try:
                assert (await anext(events)).startswith(b"event: ready")
                with psycopg.connect(dsn) as owner:
                    owner.execute(
                        "SELECT set_config('app.current_tenant', %s, true)",
                        [str(rbac_graph.organization_a)],
                    )
                    if action == "membership":
                        owner.execute(
                            "DELETE FROM clinic_app.identity_userclinicrole "
                            "WHERE user_id=%s AND clinic_id=%s",
                            [rbac_graph.shared_user, rbac_graph.clinic_a],
                        )
                    elif action == "expiry":
                        owner.execute(
                            "UPDATE clinic_app.django_session "
                            "SET expire_date=now()-interval '1 second' "
                            "WHERE session_key=%s",
                            [key],
                        )
                    else:
                        owner.execute(
                            "INSERT INTO clinic_app.identity_rolegrant "
                            "(id, organization_id, clinic_id, role, permission, "
                            "bundle_version, effect, valid_from) VALUES "
                            "(%s,%s,%s,'receptionist','appointment.read',1,'remove',now())",
                            [uuid4(), rbac_graph.organization_a, rbac_graph.clinic_a],
                        )
                # The owner transaction has committed. No Python signal/control
                # message was sent. Redis ACK orders the subsequent domain hint.
                await stream.client.publish(
                    "rt:" + topic_hash(topic), event_bytes(topic, "agenda", 9)
                )
                async with asyncio.timeout(3):
                    frame = await anext(events)
                    assert frame.startswith(b"event: closed")
                    assert json.loads(frame.split(b"data: ")[1])["kind"] == (
                        "expired" if action == "expiry" else "revoked"
                    )
                    with pytest.raises(StopAsyncIteration):
                        await anext(events)
            finally:
                await events.aclose()

        async_to_sync(exercise)()
