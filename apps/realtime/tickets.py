"""Session-bound, single-use 60-second Redis tickets; no identifiers in URLs."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from typing import Final

from apps.realtime.authorization import TopicDeniedError, validated_topics
from apps.realtime.transport import redis_client

TICKET_TTL: Final = 60
TICKET_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def issue_ticket(session_key: str, topics: tuple[str, ...]) -> str:
    """Store authority selectors privately, bounded in size and lifetime."""
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps({"session": _digest(session_key), "topics": topics})
    with redis_client() as client:
        client.set("rt-ticket:" + _digest(ticket), payload, ex=TICKET_TTL)
    return ticket


def consume_ticket(ticket: str, session_key: str) -> tuple[str, ...]:
    """Atomically burn before checking binding; replay cannot race GETDEL."""
    if TICKET_PATTERN.fullmatch(ticket) is None or not session_key:
        raise TopicDeniedError
    with redis_client() as client:
        raw = client.getdel("rt-ticket:" + _digest(ticket))
    if not isinstance(raw, bytes):
        raise TopicDeniedError
    payload = json.loads(raw)
    if not hmac.compare_digest(payload["session"], _digest(session_key)):
        raise TopicDeniedError
    return validated_topics(payload["topics"])
