"""Per-tenant fair queueing for Celery workloads.

One tenant's bulk import or AI burst must not delay another tenant's
chart saves or reminders (IS-16). Each regulated queue is metered by a
Redis token bucket keyed on ``(organization, queue)``; quotas are
published per organization through the validated ``ClinicConfiguration``
``queue_quotas`` map and read through the ``identity_queue_quotas``
resolver, so a worker needs no request or actor context.

Failure posture is explicit and asymmetric: a Redis outage fails closed
(defer) for every queue except ``clinical``, which fails open so chart
writes are never blocked by a metering dependency. Deferred tasks are
re-enqueued with a countdown through ``acquire_or_defer`` and every
deferral emits the ``clinic_fairness.deferred`` metric hook.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Protocol
from uuid import UUID

import redis
from django.core.exceptions import ValidationError
from django.db import connection

logger = logging.getLogger(__name__)

# The complete queue topology; config/celery.py declares the same names and
# tests pin the agreement. ``clinic-integrations`` predates this module and
# keeps its name for the comms outbox tasks.
QUEUE_NAMES: tuple[str, ...] = (
    "clinic-integrations",
    "clinical",
    "ai-interactive",
    "ai-batch",
    "messaging",
    "finance",
    "bulk",
)
_QUEUE_SET = frozenset(QUEUE_NAMES)

# Queues whose deferral would block patient-facing clinical work fail open
# when the metering backend is unreachable; every other queue fails closed.
FAIL_OPEN_QUEUES: frozenset[str] = frozenset({"clinical"})

# Quotas are task admissions per minute per organization per queue. The
# bucket capacity equals the quota, so a quiet tenant may burst one full
# minute of quota and is then limited to the steady refill rate.
DEFAULT_QUOTA_PER_MINUTE = 600
MAX_QUOTA_PER_MINUTE = 100_000

# Deferred tasks are re-enqueued this many seconds later.
DEFER_COUNTDOWN_SECONDS = 30

_BUCKET_KEY_PREFIX = "clinic-fair:v1"
_SOCKET_TIMEOUT_SECONDS = 1.0

# Atomic token bucket: refill at the per-minute rate, spend one token,
# expire idle buckets after roughly two full refills.
_BUCKET_LUA = """
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens') or ARGV[1])
local touched = tonumber(redis.call('HGET', KEYS[1], 'touched') or ARGV[2])
local capacity = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local rate = tonumber(ARGV[3])
tokens = math.min(capacity, tokens + (now - touched) * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'touched', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(2000 * capacity / rate))
return allowed
"""


class FairnessInputError(ValueError):
    """Reject a malformed ``fair_acquire`` call or quota value."""


class FairnessBackendError(redis.RedisError):
    """Report a metering backend that cannot serve token buckets."""


class _TaskRequest(Protocol):
    """The request attributes a bound task needs for re-enqueue."""

    args: tuple[object, ...]
    kwargs: dict[str, object]


class _DeferredTask(Protocol):
    """The bound-task surface ``acquire_or_defer`` re-enqueues through."""

    @property
    def request(self) -> _TaskRequest: ...

    def apply_async(self, **options: object) -> object: ...


def _now() -> float:
    """Return the wall clock; the bucket is shared across processes."""
    return time.time()


def _emit_metric(name: str, *, queue: str, reason: str) -> None:
    """Emit one fairness metric; labels never carry tenant identifiers.

    This is the single metric hook seam: the telemetry module (todo 11)
    or tests replace it, and the default sink is a structured log line.
    """
    logger.info("%s queue=%s reason=%s", name, queue, reason)


def validate_queue_quotas(quotas: object) -> dict[str, int]:
    """Validate one ``queue_quotas`` map and return a detached copy.

    Only known queue names and integer quotas in
    ``[1, MAX_QUOTA_PER_MINUTE]`` are accepted; anything else raises
    ``ValidationError`` so a forged or malformed map can never reach the
    stored configuration.
    """
    if not isinstance(quotas, dict):
        message = "Cotas de fila inválidas."
        raise ValidationError(message)
    normalized: dict[str, int] = {}
    for name, value in quotas.items():
        if (
            type(name) is not str
            or name not in _QUEUE_SET
            or type(value) is not int
            or not 1 <= value <= MAX_QUOTA_PER_MINUTE
        ):
            message = "Cotas de fila inválidas."
            raise ValidationError(message)
        normalized[name] = value
    return normalized


def _broker_url() -> str:
    """Read the effective broker URL through the Celery app config."""
    from config.celery import app  # noqa: PLC0415 - deferred import

    return str(app.conf.broker_url)


_clients: dict[str, redis.Redis] = {}


def _client() -> redis.Redis:
    """Return a pooled Redis client for the configured broker URL."""
    url = _broker_url()
    if not url.startswith(("redis://", "rediss://", "unix://")):
        message = "fairness metering requires a redis broker"
        raise FairnessBackendError(message)
    client = _clients.get(url)
    if client is None:
        client = redis.Redis.from_url(
            url,
            socket_timeout=_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=_SOCKET_TIMEOUT_SECONDS,
        )
        _clients[url] = client
    return client


def _organization_quotas(organization_id: UUID) -> object:
    """Read the published quota map through the resolver function.

    ``identity_queue_quotas`` is SECURITY DEFINER over the append-only
    configuration table, so workers read quotas without a request actor.
    The raw value is returned unvalidated; ``fair_acquire`` fails closed
    on any malformed shape rather than trusting the CHECK constraint.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.identity_queue_quotas(%s)",
            [str(organization_id)],
        )
        row = cursor.fetchone()
    stored = row[0] if row else None
    # No published configuration means no quotas, not a malformed map.
    if stored is None:
        return {}
    # Raw cursors return jsonb as text; decode before the shape check.
    # An undecodable value stays a non-dict and fails closed downstream.
    if isinstance(stored, str):
        with contextlib.suppress(json.JSONDecodeError):
            stored = json.loads(stored)
    return stored


def fair_acquire(*, organization_id: UUID, queue: str) -> bool:
    """Spend one token from the organization's bucket on ``queue``.

    Returns ``True`` when the task may run now and ``False`` when it must
    defer. A Redis outage defers every queue except the fail-open
    ``clinical`` queue; a missing quota falls back to
    ``DEFAULT_QUOTA_PER_MINUTE``. A malformed stored map or value fails
    closed (defer) — a stored quota can never silently grow into the
    default allowance.
    """
    if type(organization_id) is not UUID or queue not in _QUEUE_SET:
        message = "organization_id and queue are required"
        raise FairnessInputError(message)
    stored = _organization_quotas(organization_id)
    if not isinstance(stored, dict):
        _emit_metric("clinic_fairness.deferred", queue=queue, reason="malformed_quota")
        return False
    quota = stored.get(queue, DEFAULT_QUOTA_PER_MINUTE)
    if type(quota) is not int or not 1 <= quota <= MAX_QUOTA_PER_MINUTE:
        _emit_metric("clinic_fairness.deferred", queue=queue, reason="malformed_quota")
        return False
    key = f"{_BUCKET_KEY_PREFIX}:{organization_id}:{queue}"
    try:
        allowed = _client().eval(
            _BUCKET_LUA,
            1,
            key,
            quota,
            _now(),
            quota / 60.0,
        )
    except redis.RedisError:
        if queue in FAIL_OPEN_QUEUES:
            _emit_metric(
                "clinic_fairness.fail_open", queue=queue, reason="redis_unavailable"
            )
            return True
        _emit_metric(
            "clinic_fairness.deferred", queue=queue, reason="redis_unavailable"
        )
        return False
    if allowed:
        return True
    _emit_metric("clinic_fairness.deferred", queue=queue, reason="quota_exceeded")
    return False


def acquire_or_defer(task: _DeferredTask, *, organization_id: UUID, queue: str) -> bool:
    """Acquire quota or re-enqueue the task with a countdown.

    Returns ``True`` when the task body may proceed. On deferral the task
    is re-published with ``DEFER_COUNTDOWN_SECONDS`` through
    ``apply_async`` — a fresh delivery that never consumes the task's
    retry budget — and this call returns ``False`` so the body returns
    without doing the work.
    """
    if fair_acquire(organization_id=organization_id, queue=queue):
        return True
    task.apply_async(
        args=task.request.args,
        kwargs=task.request.kwargs,
        countdown=DEFER_COUNTDOWN_SECONDS,
    )
    return False
