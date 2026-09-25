"""Malformed and unavailable transport paths never pretend to be a live stream."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.realtime import transport
from apps.realtime.authorization import Subscription
from apps.realtime.stream import EventStream
from apps.realtime.views import stream_view, ticket_view
from asgiref.sync import async_to_sync
from django.http import JsonResponse
from django.test import RequestFactory
from redis.exceptions import ConnectionError as RedisConnectionError

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"[]",
        b"{}",
        b"x" * 2049,
        b'{"topics":[]}',
        b'{"topics":[1]}',
        b'{"topics":["x","x"]}',
    ],
)
def test_bad_subscription_bodies_have_one_denial(
    body: bytes, settings: SettingsWrapper
) -> None:
    settings.REALTIME_ENABLED = True
    response = ticket_view(
        RequestFactory().post("/rt/stream", body, content_type="application/json")
    )
    assert response.status_code == 403
    assert isinstance(response, JsonResponse)
    assert json.loads(response.content) == {
        "code": "access_denied",
        "message_key": "subscription_unavailable",
    }


@pytest.mark.parametrize(
    "path",
    [
        "/rt/stream",
        "/rt/stream?t=x&t=y",
        "/rt/stream?t=x&clinic=forged",
        "/rt/stream?t=x",
    ],
)
def test_bad_stream_requests_have_one_denial(
    path: str, settings: SettingsWrapper
) -> None:
    settings.REALTIME_ENABLED = True
    response = async_to_sync(stream_view)(RequestFactory().get(path))
    assert response.status_code == 403


def test_disabled_transport_reports_unavailability(settings: SettingsWrapper) -> None:
    settings.REALTIME_ENABLED = False
    factory = RequestFactory()
    assert ticket_view(factory.post("/rt/stream")).status_code == 503
    assert async_to_sync(stream_view)(factory.get("/rt/stream")).status_code == 503


def test_failed_publication_is_observable_without_echoing_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def unavailable() -> None:
        message = "SINTETICO-SENTINELA-PHI"
        raise RedisConnectionError(message)

    monkeypatch.setattr(transport, "redis_client", unavailable)
    transport.publish(f"clinic:{uuid4()}:agenda", "agenda", 1)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert "SINTETICO-SENTINELA-PHI" not in caplog.text


@pytest.mark.usefixtures("real_redis")
def test_stream_startup_timeout_closes_without_a_ready_event(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def timed_out(self: EventStream) -> None:
        raise TimeoutError

    monkeypatch.setattr(EventStream, "open", timed_out)
    stream = EventStream(
        Subscription(uuid4().hex, uuid4(), (f"clinic:{uuid4()}:agenda",))
    )

    async def exercise() -> None:
        with pytest.raises(StopAsyncIteration):
            await anext(stream.events())
        assert stream.pubsub.connection is None

    async_to_sync(exercise)()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
