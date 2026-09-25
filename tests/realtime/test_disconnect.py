"""Actual ASGI disconnect/reauthorization must release DB and broker resources."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Protocol, cast

import psycopg
import pytest
from apps.realtime import stream as stream_module
from apps.realtime.authorization import authorize_topics
from apps.realtime.stream import EventStream
from apps.realtime.tickets import issue_ticket
from apps.realtime.transport import event_bytes, redis_client, topic_hash
from asgiref.sync import async_to_sync
from asgiref.testing import ApplicationCommunicator
from django.conf import settings
from django.core.asgi import get_asgi_application
from django.db import connection
from django.test import override_settings
from redis.asyncio import Redis

from database_urls import database_url_for_name
from realtime.test_authorization import session_key
from realtime.test_stream import runtime_connection

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from apps.realtime.authorization import Subscription

    from rbac_fixtures import RbacGraph


class Communicator(Protocol):
    """Type the public surface of asgiref's unannotated test communicator."""

    @property
    def future(self) -> asyncio.Task[None]: ...

    async def send_input(self, message: dict[str, object]) -> None: ...

    def receive_output(self, timeout: float) -> Awaitable[dict[str, object]]: ...


communicator = cast(
    "Callable[[object, dict[str, object]], Communicator]", ApplicationCommunicator
)

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("real_redis"),
]


def assert_database_closed(dsn: str, application_name: str) -> None:
    with psycopg.connect(dsn) as probe:
        assert (
            probe.execute(
                "SELECT pid, state, xact_start FROM pg_stat_activity "
                "WHERE application_name=%s",
                [application_name],
            ).fetchall()
            == []
        )


async def close_application(client: Communicator, streams: list[EventStream]) -> None:
    try:
        if not client.future.done():
            await client.send_input({"type": "http.disconnect"})
        async with asyncio.timeout(5):
            await client.future
    finally:
        # Assertion/mutation failures must not leak the test's sockets.
        for stream in streams:
            await stream.close()


@pytest.fixture
def opened_streams(monkeypatch: pytest.MonkeyPatch) -> list[EventStream]:
    streams: list[EventStream] = []
    initialize = EventStream.__init__

    def observe(stream: EventStream, subscription: Subscription) -> None:
        initialize(stream, subscription)
        streams.append(stream)

    monkeypatch.setattr(EventStream, "__init__", observe)
    return streams


@pytest.mark.parametrize("reauthorize", [False, True])
def test_asgi_disconnect_leaves_no_database_or_pubsub_connection(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    reauthorize: bool,
    opened_streams: list[EventStream],
) -> None:
    key = session_key(rbac_graph)
    topic = f"clinic:{rbac_graph.clinic_a}:agenda"
    ticket = issue_ticket(session_key=key, topics=(topic,))
    clock = [0.0]
    monkeypatch.setattr(stream_module, "monotonic", lambda: clock[0])
    super_dsn = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )
    with (
        runtime_connection() as application_name,
        override_settings(
            ROOT_URLCONF="config.urls_realtime",
            MIDDLEWARE=[
                "apps.core.middleware.ResponsePrivacyMiddleware",
                "apps.core.middleware.LiveModeHaltMiddleware",
                "apps.core.middleware.ContentSecurityPolicyMiddleware",
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "django_otp.middleware.OTPMiddleware",
            ],
        ),
    ):
        application = get_asgi_application()

        async def exercise() -> None:
            checked = asyncio.Event()

            async def observe(
                *, session_key: str, topics: tuple[str, ...]
            ) -> Subscription:
                result = await authorize_topics(session_key=session_key, topics=topics)
                if clock[0] == 60:
                    checked.set()
                return result

            monkeypatch.setattr(stream_module, "authorize_topics", observe)
            client = communicator(
                application,
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": "/rt/stream",
                    "query_string": ("t=" + ticket).encode(),
                    "headers": [
                        (b"host", b"localhost"),
                        (b"cookie", ("sessionid=" + key).encode()),
                    ],
                    "server": ("127.0.0.1", 8001),
                    "client": ("127.0.0.1", 12345),
                },
            )
            try:
                await client.send_input({"type": "http.request", "body": b""})
                start = await client.receive_output(timeout=5)
                assert start["status"] == 200
                ready = await client.receive_output(timeout=5)
                assert isinstance(ready["body"], bytes)
                assert ready["body"].startswith(b"event: ready")
                assert_database_closed(super_dsn, application_name)
                if reauthorize:
                    clock[0] = 60
                    async with Redis.from_url(settings.REALTIME_REDIS_URL) as publisher:
                        # Wake the receive with a filtered frame, then await the
                        # exact periodic authorization completion before probing.
                        await publisher.publish("rt:" + topic_hash(topic), b"{}")
                        async with asyncio.timeout(5):
                            await checked.wait()
                        assert_database_closed(super_dsn, application_name)
                        await publisher.publish(
                            "rt:" + topic_hash(topic), event_bytes(topic, "agenda", 2)
                        )
                    frame = await client.receive_output(timeout=5)
                    assert isinstance(frame["body"], bytes)
                    assert frame["body"].startswith(b"data: ")
                    assert_database_closed(super_dsn, application_name)
                # Exact ASGI disconnect signal, not an elapsed-time approximation.
                await client.send_input({"type": "http.disconnect"})
                async with asyncio.timeout(5):
                    await client.future
                assert_database_closed(super_dsn, application_name)
                with redis_client() as broker:
                    assert broker.pubsub_numsub("rt:" + topic_hash(topic)) == [
                        (("rt:" + topic_hash(topic)).encode(), 0)
                    ]
            finally:
                await close_application(client, opened_streams)

        async_to_sync(exercise)()
