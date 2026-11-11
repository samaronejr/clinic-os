"""Per-client Redis token bucket for the external API.

Each API client owns one bucket whose capacity is its per-minute limit and
whose refill rate is that limit per 60 seconds, so a quiet client may burst one
minute of quota. One atomic Lua call refills, spends and reports the wait until
the next token. A metering outage fails closed: the API answers 503 with a
``Retry-After`` instead of serving unmetered partner traffic.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

import redis

_BUCKET_KEY_PREFIX: Final = "clinic-api:v1"
_SOCKET_TIMEOUT_SECONDS: Final = 1.0
OUTAGE_RETRY_SECONDS: Final = 30
# Resource calls and token issuance meter separately for the same client.
PURPOSES: Final = frozenset({"api", "token"})
TOKEN_REQUESTS_PER_MINUTE: Final = 30

# KEYS[1] bucket; ARGV capacity, now (s), refill rate (tokens/s).
# Returns {allowed, wait_ms}: wait_ms is 0 when allowed.
_BUCKET_LUA: Final = """
local capacity = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local rate = tonumber(ARGV[3])
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens') or capacity)
local touched = tonumber(redis.call('HGET', KEYS[1], 'touched') or now)
tokens = math.min(capacity, tokens + math.max(0, now - touched) * rate)
local allowed = 0
local wait_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  wait_ms = math.ceil((1 - tokens) / rate * 1000)
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'touched', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(2000 * capacity / rate))
return {allowed, wait_ms}
"""


class RateLimitUnavailableError(Exception):
    """The metering backend cannot decide; callers must fail closed."""


@dataclass(frozen=True, slots=True)
class Decision:
    """One admission decision and the whole seconds to wait when refused."""

    allowed: bool
    retry_after_seconds: int


class TokenBucket(Protocol):
    """Spend one token from a client's bucket."""

    def spend(self, *, client_id: UUID, per_minute: int, purpose: str) -> Decision:
        """Admit or refuse one request for ``client_id`` in one bucket purpose."""
        ...


def _now() -> float:
    """Return the shared wall clock seconds; tests freeze this seam."""
    return time.time()


def _broker_url() -> str:
    from config.celery import app  # noqa: PLC0415 - deferred import

    return str(app.conf.broker_url)


_clients: dict[str, redis.Redis] = {}


class RedisTokenBucket:
    """The production bucket over the configured Redis broker."""

    def _client(self) -> redis.Redis:
        url = _broker_url()
        if not url.startswith(("redis://", "rediss://", "unix://")):
            raise RateLimitUnavailableError
        client = _clients.get(url)
        if client is None:
            client = redis.Redis.from_url(
                url,
                socket_timeout=_SOCKET_TIMEOUT_SECONDS,
                socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
            )
            _clients[url] = client
        return client

    def spend(self, *, client_id: UUID, per_minute: int, purpose: str) -> Decision:
        """Run the atomic bucket script; any Redis failure is unavailable."""
        if (
            type(client_id) is not UUID
            or type(per_minute) is not int
            or per_minute < 1
            or purpose not in PURPOSES
        ):
            raise RateLimitUnavailableError
        key = f"{_BUCKET_KEY_PREFIX}:{purpose}:{client_id}"
        try:
            raw = self._client().eval(
                _BUCKET_LUA, 1, key, per_minute, _now(), per_minute / 60.0
            )
        except redis.RedisError as error:
            raise RateLimitUnavailableError from error
        if not isinstance(raw, list) or len(raw) != 2:  # noqa: PLR2004 - script shape
            raise RateLimitUnavailableError
        allowed, wait_ms = (int(value) for value in raw)
        return Decision(
            allowed=allowed == 1,
            retry_after_seconds=0
            if allowed == 1
            else max(1, math.ceil(wait_ms / 1000)),
        )


_backend: TokenBucket = RedisTokenBucket()


def bucket() -> TokenBucket:
    """Return the configured bucket; tests may install a different one."""
    return _backend
