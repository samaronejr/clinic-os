"""Opaque Redis channels and bounded, metadata-only invalidation messages."""

from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache, partial
from os import getpid, register_at_fork
from typing import TYPE_CHECKING, Final
from weakref import WeakSet

from django.conf import settings
from django.db import connection, transaction
from redis import ConnectionPool, Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from apps.realtime.topics import TOPIC_PATTERN

if TYPE_CHECKING:
    from collections.abc import Callable

    from redis.connection import ConnectionInterface

EVENT_KINDS: Final = frozenset(
    {
        "agenda",
        "inbox",
        "queue",
        "messages",
        "ai_job",
        "teleconsult",
        "ready",
        "revoked",
        "expired",
        "halted",
    }
)
MAX_VERSION: Final = 2**53 - 1
logger = logging.getLogger(__name__)


def topic_hash(topic: str) -> str:
    """Domain-separate the deployment secret; raw selectors never reach channels."""
    if TOPIC_PATTERN.fullmatch(topic) is None:
        message = "invalid realtime topic"
        raise ValueError(message)
    return hashlib.sha256(
        (topic + settings.REALTIME_TOPIC_SECRET).encode("utf-8")
    ).hexdigest()


def event_bytes(topic: str, kind: str, version: int) -> bytes:
    """Serialize exactly three keys; kind cannot be free text or an identifier."""
    if (
        kind not in EVENT_KINDS
        or type(version) is not int
        or not 0 <= version <= MAX_VERSION
    ):
        message = "invalid realtime event"
        raise ValueError(message)
    return json.dumps(
        {"topic_hash": topic_hash(topic), "kind": kind, "version": version},
        separators=(",", ":"),
    ).encode("ascii")


def redis_client() -> Redis:
    """Use a bounded connection; failure must not hang a committed request."""
    return Redis.from_url(
        settings.REALTIME_REDIS_URL,
        socket_connect_timeout=1,
        socket_timeout=1,
    )


_publisher_connections: WeakSet[ConnectionInterface] = WeakSet()


class _PublisherConnectionPool(ConnectionPool):
    def make_connection(self) -> ConnectionInterface:
        """Track connections even while checked out or after cache eviction."""
        conn = super().make_connection()
        _publisher_connections.add(conn)
        return conn


@lru_cache(maxsize=1)
def _publisher_pool(url: str, _pid: int) -> ConnectionPool:
    # Keep hot sockets local to the process and the active broker URL. Do not
    # replay a PUBLISH whose acknowledgement was lost.
    return _PublisherConnectionPool.from_url(
        url,
        socket_connect_timeout=1,
        socket_timeout=1,
        retry=Retry(NoBackoff(), 0),
    )


def _discard_inherited_publisher_sockets() -> None:
    for conn in tuple(_publisher_connections):
        # Redis-py skips shutdown on a connection PID mismatch. Disconnect
        # individual connections before clearing the pool, without pool locks.
        disconnect: Callable[[], None] = conn.disconnect
        disconnect()
    _publisher_connections.clear()
    _publisher_pool.cache_clear()


register_at_fork(after_in_child=_discard_inherited_publisher_sockets)


def publish(*, topic: str, kind: str, version: int) -> None:
    """Publish a refetch hint after commit; never interrupt committed domain work.

    Redis loss is explicitly observable and recoverable by client polling. It
    cannot roll back a successful booking, and is not a durable event receipt.
    """
    payload = event_bytes(topic, kind, version)
    if connection.in_atomic_block:
        message = "realtime publication requires a committed transaction"
        raise RuntimeError(message)
    try:
        pool = _publisher_pool(settings.REALTIME_REDIS_URL, getpid())
        # An explicitly supplied pool stays open when this client exits.
        with Redis(connection_pool=pool) as client:
            client.publish("rt:" + topic_hash(topic), payload)
    except RedisError:
        logger.warning("realtime publication unavailable; polling required")


def publish_on_commit(*, topic: str, kind: str, version: int) -> None:
    """Register the only domain publication path; rollback discards the hint."""
    event_bytes(topic, kind, version)
    if settings.REALTIME_ENABLED:
        transaction.on_commit(partial(publish, topic=topic, kind=kind, version=version))
