"""Async stream lifetime: short authorization, no database work while idle."""

from __future__ import annotations

import asyncio
import json
import logging
from time import monotonic
from typing import TYPE_CHECKING, Final, Protocol, cast

from django.conf import settings
from django.utils import timezone
from ops.release.activation import LiveModeHaltedError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from apps.realtime.authorization import (
    Subscription,
    TopicDeniedError,
    TopicExpiredError,
    authorize_topics,
)
from apps.realtime.transport import EVENT_KINDS, MAX_VERSION, event_bytes, topic_hash

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from redis.asyncio.client import PubSub

REAUTHORIZE_SECONDS: Final = 60
CONNECTION_SECONDS: Final = 600
HEARTBEAT_SECONDS: Final = 15
AUTHORIZATION_TIMEOUT_SECONDS: Final = 5
MAX_EVENT_BYTES: Final = 256
logger = logging.getLogger(__name__)


def _event(raw: object, allowed: frozenset[str]) -> bytes | None:
    """Treat broker input as untrusted; never relay arbitrary JSON or text."""
    if not isinstance(raw, bytes) or len(raw) > MAX_EVENT_BYTES:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"topic_hash", "kind", "version"}
        or not isinstance(value["topic_hash"], str)
        or value["topic_hash"] not in allowed
        or not isinstance(value["kind"], str)
        or value["kind"] not in EVENT_KINDS
        or type(value["version"]) is not int
        or not 0 <= value["version"] <= MAX_VERSION
    ):
        return None
    return (
        b"data: " + json.dumps(value, separators=(",", ":")).encode("ascii") + b"\n\n"
    )


class _AsyncClose(Protocol):
    async def aclose(self) -> None: ...


class EventStream:
    """One Redis subscription owned by one HTTP stream, closed on every exit."""

    def __init__(self, subscription: Subscription) -> None:
        """Allocate no database connection; Redis connects during open."""
        self.subscription = subscription
        self.client = Redis.from_url(
            settings.REALTIME_REDIS_URL, socket_connect_timeout=1
        )
        self.pubsub: PubSub = self.client.pubsub()
        self.hashes = frozenset(topic_hash(topic) for topic in subscription.topics)
        self.revocation = "authz:user:" + str(
            subscription.patient_session_id or subscription.user_id
        )
        self.control = frozenset(
            {topic_hash(self.revocation), topic_hash("authz:halt")}
        )
        self.opened_at = monotonic()

    async def open(self) -> None:
        """Await subscription ACKs before rechecking authority to close the race."""
        channels = tuple(
            "rt:" + digest for digest in sorted(self.hashes | self.control)
        )
        try:
            async with asyncio.timeout(AUTHORIZATION_TIMEOUT_SECONDS):
                await self.pubsub.subscribe(*channels)
                for _ in channels:
                    await self.pubsub.get_message(
                        ignore_subscribe_messages=False, timeout=None
                    )
                await self._reauthorize()
        except BaseException:
            await self.close()
            raise

    async def _reauthorize(self) -> None:
        remaining = self.opened_at + CONNECTION_SECONDS - monotonic()
        if remaining <= 0:
            raise TopicExpiredError
        async with asyncio.timeout(min(AUTHORIZATION_TIMEOUT_SECONDS, remaining)):
            self.subscription = await authorize_topics(
                session_key=self.subscription.session_key,
                topics=self.subscription.topics,
            )

    def _session_seconds(self) -> float:
        expiry = self.subscription.expires_at
        return (
            (expiry - timezone.now()).total_seconds() if expiry else CONNECTION_SECONDS
        )

    async def _receive(self, next_check: float) -> dict[str, object] | None:
        now = monotonic()
        remaining = self.opened_at + CONNECTION_SECONDS - now
        timeout = max(
            0,
            min(
                HEARTBEAT_SECONDS, next_check - now, remaining, self._session_seconds()
            ),
        )
        message = await self.pubsub.get_message(
            ignore_subscribe_messages=True, timeout=timeout
        )
        if monotonic() >= self.opened_at + CONNECTION_SECONDS:
            raise TopicExpiredError
        if self._session_seconds() <= 0:
            await self._reauthorize()
        return cast("dict[str, object] | None", message)

    async def close(self) -> None:
        """Release the pub/sub socket on denial, disconnect, timeout or cancellation."""
        # redis 8 types Redis.aclose but omits the PubSub.aclose annotation.
        await cast("_AsyncClose", self.pubsub).aclose()
        await self.client.aclose()

    def _closed(self, kind: str) -> bytes:
        return (
            b"event: closed\ndata: " + event_bytes(self.revocation, kind, 0) + b"\n\n"
        )

    async def events(self) -> AsyncGenerator[bytes]:
        """Recheck even under a busy topic; the deadline never slides on events."""
        next_check = self.opened_at + REAUTHORIZE_SECONDS
        try:
            # Allocate sockets only after body iteration starts. A client that
            # disconnects before the first yield leaves no orphan subscription.
            await self.open()
            yield (
                b"event: ready\ndata: "
                + event_bytes(self.revocation, "ready", 0)
                + b"\n\n"
            )
            while True:
                now = monotonic()
                if now >= self.opened_at + CONNECTION_SECONDS:
                    yield self._closed("expired")
                    return
                if now >= next_check or self._session_seconds() <= 0:
                    await self._reauthorize()
                    next_check = monotonic() + REAUTHORIZE_SECONDS
                message = await self._receive(next_check)
                if message is None:
                    yield b": heartbeat\n\n"
                    continue
                channel = message["channel"]
                if channel in {("rt:" + item).encode() for item in self.control}:
                    await self._reauthorize()
                    yield self._closed("revoked")
                    return
                if frame := _event(message["data"], self.hashes):
                    # Also fences raw SQL changes and lost/queued control messages.
                    # No domain frame is released on the strength of old authority.
                    await self._reauthorize()
                    yield frame
        except TopicExpiredError:
            yield self._closed("expired")
        except (TopicDeniedError, LiveModeHaltedError):
            yield self._closed("revoked")
        except (RedisError, TimeoutError):
            logger.warning("realtime subscription unavailable; polling required")
        finally:
            await self.close()
