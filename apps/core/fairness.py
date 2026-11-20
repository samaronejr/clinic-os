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
import os
import threading
import time
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

import redis
from django.core.exceptions import ValidationError
from django.db import connection
from redis.exceptions import NoScriptError

if TYPE_CHECKING:
    from collections.abc import Callable

    from redis.client import PubSub

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
_DEFER_LOG_INTERVAL_SECONDS = 1.0
_defer_log_times: dict[tuple[str, str], float] = {}
_defer_log_lock = threading.Lock()
_RESOLVER_OWNER_PREFIX = uuid4().hex
_RESOLVER_BUSY = 2
_RESOLVER_WAIT_MILLISECONDS = 500
_RESOLVER_CHANNEL = f"{_BUCKET_KEY_PREFIX}:resolved"

# Keep the two-minute idle lifetime, with up to one second of extra retention
# so hot buckets don't write expiry state on every decision. Use Redis's TTL,
# not the injectable bucket clock; an absent TTL also needs renewal.
_KEEP_BUCKET_LUA = """
local function keep_bucket(key)
  if redis.call('PTTL', key) < 120000 then
    redis.call('PEXPIRE', key, 121000)
  end
end
"""

# This is a negative preflight and an advisory in-flight decision slot,
# never an admission or a quota cache.
# For nonnegative elapsed time, no legal rate can refill faster than MAX/60.
# Reject only below one token even at that upper bound. Missing state or a
# backwards clock must reach the fresh resolver and the actual bucket.
# Busy callers wait and recheck, never refuse on another caller's result.
# Slot expiry only permits duplicate fresh reads; it never grants authority.
# Redis time drives the slot independently of the injectable bucket clock.
# Completion publications contain only a bucket key, never authority.
# Register before rechecking credit; missed signals use the lease backstop.
_CREDIT_POSSIBLE_LUA = (
    _KEEP_BUCKET_LUA
    + """
local state = redis.call('HMGET', KEYS[1],
                         'tokens', 'touched', 'resolving', 'resolve_until')
local tokens = tonumber(state[1])
local touched = tonumber(state[2])
local now = tonumber(ARGV[1])
if tokens and touched and now >= touched and
   tokens + (now - touched) * tonumber(ARGV[2]) < 1 then
  keep_bucket(KEYS[1])
  return 0
end
local clock = redis.call('TIME')
local server_now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
if state[3] and state[3] ~= ARGV[3] and
   tonumber(state[4]) > server_now then
  redis.call('HSET', KEYS[1], 'notify', 1)
  return 2
end
redis.call('HSET', KEYS[1], 'resolving', ARGV[3],
           'resolve_until', server_now + 1)
keep_bucket(KEYS[1])
return 1
"""
)

_RELEASE_RESOLVER_LUA = f"""
local state = redis.call('HMGET', KEYS[1], 'resolving', 'notify')
if state[1] == ARGV[1] then
  redis.call('HDEL', KEYS[1], 'resolving', 'resolve_until', 'notify')
  if state[2] then
    redis.call('PUBLISH', '{_RESOLVER_CHANNEL}', KEYS[1])
  end
end
return 0
"""

# Atomic token bucket: refill at the per-minute rate, spend one token,
# expire idle buckets after roughly two full refills.
_BUCKET_LUA = (
    _KEEP_BUCKET_LUA
    + f"""
local state = redis.call('HMGET', KEYS[1],
                         'tokens', 'touched', 'resolving', 'notify')
local tokens = tonumber(state[1] or ARGV[1])
local touched = tonumber(state[2] or ARGV[2])
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
keep_bucket(KEYS[1])
if state[3] == ARGV[4] then
  redis.call('HDEL', KEYS[1], 'resolving', 'resolve_until', 'notify')
  if state[4] then
    redis.call('PUBLISH', '{_RESOLVER_CHANNEL}', KEYS[1])
  end
end
return allowed
"""
)


class FairnessInputError(ValueError):
    """Reject a malformed ``fair_acquire`` call or quota value."""


class FairnessBackendError(redis.RedisError):
    """Report a metering backend that cannot serve token buckets."""


class _ResolverWaiter(threading.local):
    def __init__(self) -> None:
        self.key: str | None = None
        self.event = threading.Event()


class _ResolverSignals:
    """Own one subscriber in its reader thread and no cached authority."""

    def __init__(self, client: redis.Redis) -> None:
        self.pid = os.getpid()
        self.error: redis.RedisError | None = None
        self._open_subscription: Callable[[], PubSub] = client.pubsub
        self._ready = threading.Event()
        self._stop = threading.Event()
        self.stopped = threading.Event()
        self._lock = threading.Lock()
        self._local = _ResolverWaiter()
        self._waiters: WeakKeyDictionary[threading.Event, bytes] = WeakKeyDictionary()
        self._thread = threading.Thread(
            target=self._read, name="fairness-completions", daemon=True
        )
        self._thread.start()

    def _wake(self, key: bytes | None = None) -> None:
        with self._lock:
            for event, bucket in self._waiters.items():
                if key is None or bucket == key:
                    event.set()

    def _connected(self, _connection: object) -> None:
        self._ready.clear()
        self._wake()

    def _read(self) -> None:
        try:
            # Only this thread creates, reads and closes the PubSub object.
            with (
                self._open_subscription() as subscription,
                contextlib.ExitStack() as cleanup,
            ):
                subscription.subscribe(_RESOLVER_CHANNEL)
                connection = subscription.connection
                if connection is None:
                    message = "completion subscription has no connection"
                    raise FairnessBackendError(message)
                connection.register_connect_callback(self._connected)
                cleanup.callback(
                    connection.deregister_connect_callback, self._connected
                )
                while not self._stop.is_set():
                    message = subscription.get_message(
                        timeout=_RESOLVER_WAIT_MILLISECONDS / 1000.0
                    )
                    if message is None:
                        continue
                    if message["type"] == "subscribe":
                        self._wake()
                        self._ready.set()
                    elif message["type"] == "message":
                        data = message["data"]
                        if isinstance(data, str):
                            data = data.encode()
                        if isinstance(data, bytes):
                            self._wake(data)
        except redis.RedisError as error:
            self.error = error
            self._ready.set()
            self._wake()
        finally:
            self.stopped.set()

    def check(self) -> None:
        """Make failure persistent even when a caller clears its event."""
        if self.error is not None:
            raise self.error
        if self.stopped.is_set():
            message = "completion subscriber stopped"
            raise FairnessBackendError(message)

    def ready(self) -> None:
        """Require a subscription acknowledgement before a BUSY recheck."""
        if not self._ready.wait(timeout=_SOCKET_TIMEOUT_SECONDS):
            self.check()
            message = "completion subscription did not become ready"
            raise FairnessBackendError(message)
        self.check()

    def register(self, key: str) -> threading.Event:
        """Reuse one registration per live thread, scoped to this subscriber."""
        waiter = self._local
        if waiter.key != key:
            with self._lock:
                self._waiters[waiter.event] = key.encode()
            waiter.key = key
        return waiter.event

    def wait(self, event: threading.Event) -> None:
        """Wait for a signal or a bounded lease recheck, never an allowance."""
        self.ready()
        event.wait(timeout=_RESOLVER_WAIT_MILLISECONDS / 1000.0)
        self.check()

    def close(self) -> None:
        """Let the reader close its own subscription before releasing it."""
        self._stop.set()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            message = "completion subscriber did not stop"
            raise FairnessBackendError(message)


class _ResolverRegistry:
    def __init__(self) -> None:
        self.clients: dict[int, _ResolverSignals] = {}
        self.lock = threading.Lock()

    def reset(self) -> None:
        """Discard inherited readers, registrations and locks after fork."""
        self.clients = {}
        self.lock = threading.Lock()


_resolver_registry = _ResolverRegistry()

os.register_at_fork(after_in_child=_resolver_registry.reset)


def _signals_for(client: redis.Redis) -> _ResolverSignals:
    with _resolver_registry.lock:
        signals = _resolver_registry.clients.get(id(client))
        if (
            signals is None
            or signals.pid != os.getpid()
            or signals.error is not None
            or signals.stopped.is_set()
        ):
            signals = _ResolverSignals(client)
            _resolver_registry.clients[id(client)] = signals
    signals.ready()
    return signals


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
    or tests replace it. The default log sink limits deferrals to one line
    per second per queue/reason, independent of organization count. The hook
    itself still runs for every deferral; it holds no tenant identifiers.
    """
    if name == "clinic_fairness.deferred":
        key = (queue, reason)
        now = time.monotonic()
        with _defer_log_lock:
            previous = _defer_log_times.get(key)
            if previous is not None and now - previous < _DEFER_LOG_INTERVAL_SECONDS:
                return
            _defer_log_times[key] = now
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
_script_hashes: dict[str, str] = {}


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


def _redis_unavailable(queue: str) -> bool:
    if queue in FAIL_OPEN_QUEUES:
        _emit_metric(
            "clinic_fairness.fail_open", queue=queue, reason="redis_unavailable"
        )
        return True
    _emit_metric("clinic_fairness.deferred", queue=queue, reason="redis_unavailable")
    return False


def _evaluate(client: redis.Redis, source: str, key: str, *args: str | float) -> object:
    """Cache executable code, never quota data; reload only on NOSCRIPT."""
    sha = _script_hashes.get(source)
    if sha is None:
        sha = str(client.register_script(source).sha)
        _script_hashes[source] = sha
    try:
        return client.evalsha(sha, 1, key, *args)
    except NoScriptError:
        client.script_load(source)
        return client.evalsha(sha, 1, key, *args)


def _claim_decision(client: redis.Redis, key: str, owner: str) -> bool:
    """Block on completion, then recheck credit with no reused authority."""
    signals = _resolver_registry.clients.get(id(client))
    if signals is not None and (
        signals.pid != os.getpid()
        or signals.error is not None
        or signals.stopped.is_set()
    ):
        signals = None
    event = signals.register(key) if signals is not None else None
    contended = False
    while True:
        if signals is not None:
            if contended:
                signals.ready()
            signals.check()
            # Clear before checking credit: a completion after that check
            # stays set until wait, including completion before wait begins.
            if event is not None:
                event.clear()
        decision = _evaluate(
            client,
            _CREDIT_POSSIBLE_LUA,
            key,
            _now(),
            MAX_QUOTA_PER_MINUTE / 60.0,
            owner,
        )
        if decision != _RESOLVER_BUSY:
            return bool(decision)
        contended = True
        if signals is None:
            signals = _signals_for(client)
            event = signals.register(key)
            # The first BUSY observation preceded registration. Recheck
            # after registration before relying on a future publication.
            continue
        signals.wait(signals.register(key))


def fair_acquire(*, organization_id: UUID, queue: str) -> bool:
    """Spend one token from the organization's bucket on ``queue``.

    Returns ``True`` when the task may run now and ``False`` when it must
    defer. A Redis outage defers every queue except the fail-open
    ``clinical`` queue; a missing quota falls back to
    ``DEFAULT_QUOTA_PER_MINUTE``. A malformed stored map or value fails
    closed (defer) — a stored quota can never silently grow into the
    default allowance. An exhausted bucket may refuse before SQL only when
    even the maximum legal refill cannot admit. Every possible admission
    still reads the current published map and spends atomically at that
    quota. Concurrent possible-admission checks serialize per bucket and
    recheck credit after the current decision finishes. A busy slot never
    causes a refusal, and no caller reuses another caller's quota result.
    There is no quota cache or stale-grant window.
    """
    if type(organization_id) is not UUID or queue not in _QUEUE_SET:
        message = "organization_id and queue are required"
        raise FairnessInputError(message)
    key = f"{_BUCKET_KEY_PREFIX}:{organization_id}:{queue}"
    owner = f"{_RESOLVER_OWNER_PREFIX}:{threading.get_native_id()}"
    client = None
    decision = None
    # Validate the map first even on outage: malformed clinical quotas
    # remain fail-closed rather than taking the Redis fail-open path.
    try:
        client = _client()
        decision = _claim_decision(client, key, owner)
    except redis.RedisError:
        decision = None
    if decision is False:
        _emit_metric("clinic_fairness.deferred", queue=queue, reason="quota_exceeded")
        return False
    try:
        stored = _organization_quotas(organization_id)
        if not isinstance(stored, dict):
            _emit_metric(
                "clinic_fairness.deferred", queue=queue, reason="malformed_quota"
            )
            return False
        quota = stored.get(queue, DEFAULT_QUOTA_PER_MINUTE)
        if type(quota) is not int or not 1 <= quota <= MAX_QUOTA_PER_MINUTE:
            _emit_metric(
                "clinic_fairness.deferred", queue=queue, reason="malformed_quota"
            )
            return False
        if decision is None or client is None:
            return _redis_unavailable(queue)
        try:
            allowed = _evaluate(
                client,
                _BUCKET_LUA,
                key,
                quota,
                _now(),
                quota / 60.0,
                owner,
            )
        except redis.RedisError:
            # A lost backend leaves only a one-second advisory slot. Do not
            # retry it for cleanup or delay the existing outage policy.
            decision = None
            return _redis_unavailable(queue)
        decision = False  # The bucket script released the slot atomically.
        if not allowed:
            _emit_metric(
                "clinic_fairness.deferred", queue=queue, reason="quota_exceeded"
            )
        return bool(allowed)
    finally:
        if decision is True and client is not None:
            # Malformed authority or a DB failure must not strand waiters.
            # Cleanup cannot change an authority result; expiry is the backstop.
            with contextlib.suppress(redis.RedisError):
                _evaluate(client, _RELEASE_RESOLVER_LUA, key, owner)


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
