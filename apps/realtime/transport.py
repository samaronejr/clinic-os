"""Opaque Redis channels and bounded, metadata-only invalidation messages."""

from __future__ import annotations

import hashlib
import json
import logging
from functools import partial
from typing import Final

from django.conf import settings
from django.db import connection, transaction
from redis import Redis
from redis.exceptions import RedisError

from apps.realtime.topics import TOPIC_PATTERN

EVENT_KINDS: Final = frozenset(
    {
        "agenda",
        "inbox",
        "queue",
        "messages",
        "ai_job",
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
        with redis_client() as client:
            client.publish("rt:" + topic_hash(topic), payload)
    except RedisError:
        logger.warning("realtime publication unavailable; polling required")


def publish_on_commit(*, topic: str, kind: str, version: int) -> None:
    """Register the only domain publication path; rollback discards the hint."""
    event_bytes(topic, kind, version)
    if settings.REALTIME_ENABLED:
        transaction.on_commit(partial(publish, topic=topic, kind=kind, version=version))
