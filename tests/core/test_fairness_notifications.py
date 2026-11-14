"""Real Redis completion signals and independent Lua equivalence checks."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import redis
from apps.core import fairness
from django.db import DatabaseError

from core.test_fairness import GATED, fairness_broker, frozen_clock, metered_broker

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from uuid import UUID

    from redis.client import PubSub

pytestmark = [pytest.mark.django_db(transaction=True), GATED]
__all__ = ["fairness_broker", "frozen_clock", "metered_broker"]


def _close_subscribers() -> None:
    for signals in tuple(fairness._resolver_registry.clients.values()):
        signals.close()
    fairness._resolver_registry.reset()


@pytest.fixture(autouse=True)
def isolated_subscribers() -> Iterator[None]:
    _close_subscribers()
    yield
    _close_subscribers()


# Independent pre-notification algorithms, retained as the equivalence oracle.
LEGACY_CREDIT = """
local state = redis.call('HMGET', KEYS[1], 'tokens', 'touched')
local tokens = tonumber(state[1])
local touched = tonumber(state[2])
local now = tonumber(ARGV[1])
if tokens and touched and now >= touched and
   tokens + (now - touched) * tonumber(ARGV[2]) < 1 then
  redis.call('PEXPIRE', KEYS[1], 120000)
  return 0
end
local slot = redis.call('HMGET', KEYS[1], 'resolving', 'resolve_until')
local clock = redis.call('TIME')
local server_now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
if slot[1] and slot[1] ~= ARGV[3] and tonumber(slot[2]) > server_now then
  return 2
end
redis.call('HSET', KEYS[1], 'resolving', ARGV[3],
           'resolve_until', server_now + 1)
redis.call('PEXPIRE', KEYS[1], 120000)
return 1
"""

LEGACY_BUCKET = """
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
if redis.call('HGET', KEYS[1], 'resolving') == ARGV[4] then
  redis.call('HDEL', KEYS[1], 'resolving', 'resolve_until')
end
return allowed
"""


def test_registered_completion_before_wait_fans_out_without_streams(
    metered_broker: str, frozen_clock: dict[str, float]
) -> None:
    client = fairness._client()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{uuid4()}:ai-batch"
    signals = fairness._signals_for(client)
    barrier = threading.Barrier(5, timeout=10)

    def register(bucket: str) -> threading.Event:
        event = signals.register(bucket)
        barrier.wait()
        return event

    args = (frozen_clock["t"], fairness.MAX_QUOTA_PER_MINUTE / 60.0)
    assert client.eval(fairness._CREDIT_POSSIBLE_LUA, 1, key, *args, "leader") == 1
    client.hset(key, "resolve_until", 3_000_000_000)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(register, key) for _ in range(3)]
        other = pool.submit(register, f"{key}:other")
        barrier.wait()
        events = [future.result(timeout=10) for future in futures]
        other_event = other.result(timeout=10)
        assert len({id(event) for event in events}) == 3
        for owner in ("waiter-a", "waiter-b", "waiter-c"):
            assert client.eval(fairness._CREDIT_POSSIBLE_LUA, 1, key, *args, owner) == 2
        client.eval(fairness._RELEASE_RESOLVER_LUA, 1, key, "leader")
        for event in events:
            assert event.wait(timeout=10)
            event.clear()
        assert not other_event.is_set()
        assert not client.exists(f"{key}:completed")
        assert fairness._signals_for(client) is signals
        assert client.pubsub_numsub(fairness._RESOLVER_CHANNEL) == [
            (fairness._RESOLVER_CHANNEL.encode(), 1)
        ]

        assert client.eval(fairness._CREDIT_POSSIBLE_LUA, 1, key, *args, "next") == 1
        client.eval(fairness._RELEASE_RESOLVER_LUA, 1, key, "leader")
        assert client.hget(key, "resolving") == b"next"
        assert not any(event.is_set() for event in events)
        client.hset(key, "resolve_until", 3_000_000_000)
        assert client.eval(fairness._CREDIT_POSSIBLE_LUA, 1, key, *args, "waiter") == 2
        client.eval(fairness._RELEASE_RESOLVER_LUA, 1, key, "next")
        assert all(event.wait(timeout=10) for event in events)


def test_waiter_blocks_and_rechecks_fresh_authority(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = uuid4()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization}:ai-batch"
    client = fairness._client()
    signals = fairness._signals_for(client)
    entered, release, blocked, read_signal = (threading.Event() for _ in range(4))
    quota = {"value": 3}
    reads: list[int] = []
    credit_calls: list[int] = []
    original_evaluate = fairness._evaluate
    original_wait = signals.wait

    def evaluate(
        backend: redis.Redis, source: str, bucket: str, *args: str | float
    ) -> object:
        if source == fairness._CREDIT_POSSIBLE_LUA:
            credit_calls.append(threading.get_ident())
        return original_evaluate(backend, source, bucket, *args)

    def resolve(_organization: UUID) -> object:
        value = quota["value"]
        reads.append(value)
        if len(reads) == 1:
            client.hset(key, "resolve_until", 3_000_000_000)
            entered.set()
            assert release.wait(timeout=10)
        return {"ai-batch": value}

    def wait_for_signal(event: threading.Event) -> None:
        blocked.set()
        assert read_signal.wait(timeout=10)
        original_wait(event)

    monkeypatch.setattr(fairness, "_evaluate", evaluate)
    monkeypatch.setattr(fairness, "_organization_quotas", resolve)
    monkeypatch.setattr(signals, "wait", wait_for_signal)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(
            fairness.fair_acquire, organization_id=organization, queue="ai-batch"
        )
        try:
            assert entered.wait(timeout=10)
            waiter = pool.submit(
                fairness.fair_acquire, organization_id=organization, queue="ai-batch"
            )
            assert blocked.wait(timeout=10)
            assert reads == [3]
            # One owner check and one BUSY check; waiting is not a retry loop.
            assert len(credit_calls) == 2
            quota["value"] = 1
            release.set()
            assert leader.result(timeout=10)
        finally:
            release.set()
            read_signal.set()
        assert waiter.result(timeout=10)
    assert reads == [3, 1]
    assert client.hget(key, "tokens") == b"0"


def test_completion_during_initial_registration_requires_fresh_recheck(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = uuid4()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization}:ai-batch"
    client = fairness._client()
    client.hset(key, mapping={"resolving": "leader", "resolve_until": 3_000_000_000})
    original = fairness._evaluate
    completed: list[bool] = []
    reads: list[UUID] = []

    def evaluate(
        backend: redis.Redis, source: str, bucket: str, *args: str | float
    ) -> object:
        result = original(backend, source, bucket, *args)
        if source == fairness._CREDIT_POSSIBLE_LUA and result == 2 and not completed:
            assert not fairness._resolver_registry.clients
            original(client, fairness._RELEASE_RESOLVER_LUA, key, "leader")
            completed.append(True)
        return result

    def resolve(org: UUID) -> object:
        reads.append(org)
        return {"ai-batch": 1}

    def forbidden_wait(
        _signals: fairness._ResolverSignals, event: threading.Event
    ) -> None:
        pytest.fail("a completion lost before registration stranded a fresh decision")

    monkeypatch.setattr(fairness, "_evaluate", evaluate)
    monkeypatch.setattr(fairness, "_organization_quotas", resolve)
    monkeypatch.setattr(fairness._ResolverSignals, "wait", forbidden_wait)
    assert fairness.fair_acquire(organization_id=organization, queue="ai-batch")
    assert completed == [True]
    assert reads == [organization]


def test_missed_completion_uses_lease_recheck_and_fresh_authority(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = uuid4()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization}:ai-batch"
    client = fairness._client()
    signals = fairness._signals_for(client)
    client.hset(key, mapping={"resolving": "leader", "resolve_until": 3_000_000_000})
    dropped = threading.Event()
    reads: list[UUID] = []
    waits: list[threading.Event] = []
    original_wake = signals._wake

    def wake(bucket: bytes | None = None) -> None:
        if bucket == key.encode():
            dropped.set()
        else:
            original_wake(bucket)

    def lease_timeout(event: threading.Event) -> None:
        waits.append(event)
        client.eval(fairness._RELEASE_RESOLVER_LUA, 1, key, "leader")
        assert dropped.wait(timeout=10)
        assert not event.is_set()
        # A missed signal takes the bounded timeout path, not a refusal.

    def resolve(org: UUID) -> object:
        reads.append(org)
        return {"ai-batch": 1}

    monkeypatch.setattr(signals, "_wake", wake)
    monkeypatch.setattr(signals, "wait", lease_timeout)
    monkeypatch.setattr(fairness, "_organization_quotas", resolve)
    assert fairness.fair_acquire(organization_id=organization, queue="ai-batch")
    assert len(waits) == 1
    assert reads == [organization]


def test_subscriber_failure_reconnects_with_new_same_key_registration(
    metered_broker: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = fairness._client()
    fail_read = threading.Event()
    original_pubsub: Callable[[], PubSub] = client.pubsub

    def pubsub() -> PubSub:
        subscription = original_pubsub()
        original_get = subscription.get_message

        def receive(*, timeout: float) -> object:
            if fail_read.is_set():
                message = "synthetic subscriber disconnect"
                raise redis.ConnectionError(message)
            return original_get(timeout=timeout)

        monkeypatch.setattr(subscription, "get_message", receive)
        return subscription

    monkeypatch.setattr(client, "pubsub", pubsub)
    old = fairness._signals_for(client)
    key = f"{fairness._BUCKET_KEY_PREFIX}:{uuid4()}:ai-batch"
    event = old.register(key)
    fail_read.set()
    client.publish(fairness._RESOLVER_CHANNEL, "reader-cut-point")
    assert old.stopped.wait(timeout=10)
    assert event.is_set()
    event.clear()
    with pytest.raises(redis.ConnectionError, match="synthetic subscriber disconnect"):
        old.check()
    fail_read.clear()
    replacement = fairness._signals_for(client)
    new_event = replacement.register(key)
    assert replacement is not old
    assert new_event is not event
    client.publish(fairness._RESOLVER_CHANNEL, key)
    assert new_event.wait(timeout=10)
    assert client.pubsub_numsub(fairness._RESOLVER_CHANNEL) == [
        (fairness._RESOLVER_CHANNEL.encode(), 1)
    ]


def test_reconnect_ack_precedes_contended_authority_recheck(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = fairness._client()
    organization = uuid4()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization}:ai-batch"
    requested, reconnected, permit_ack, waiting_ack = (
        threading.Event() for _ in range(4)
    )
    holder: dict[str, fairness._ResolverSignals] = {}
    reads: list[UUID] = []
    original_pubsub: Callable[[], PubSub] = client.pubsub

    def pubsub() -> PubSub:
        subscription = original_pubsub()
        original_get = subscription.get_message

        def receive(*, timeout: float) -> object:
            if requested.is_set() and not reconnected.is_set():
                holder["signals"]._connected(None)
                reconnected.set()
                assert permit_ack.wait(timeout=10)
                return {"type": "subscribe", "data": 1}
            return original_get(timeout=timeout)

        monkeypatch.setattr(subscription, "get_message", receive)
        return subscription

    monkeypatch.setattr(client, "pubsub", pubsub)
    signals = fairness._signals_for(client)
    holder["signals"] = signals
    event = signals.register(key)
    client.hset(key, mapping={"resolving": "leader", "resolve_until": 3_000_000_000})
    requested.set()
    client.publish(fairness._RESOLVER_CHANNEL, "reconnect-cut-point")
    assert reconnected.wait(timeout=10)
    assert not signals._ready.is_set()
    assert event.is_set()
    event.clear()
    original_ready = signals.ready

    def ready() -> None:
        if not signals._ready.is_set():
            waiting_ack.set()
        original_ready()

    def resolve(org: UUID) -> object:
        reads.append(org)
        return {"ai-batch": 1}

    monkeypatch.setattr(signals, "ready", ready)
    monkeypatch.setattr(fairness, "_organization_quotas", resolve)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiter = pool.submit(
            fairness.fair_acquire, organization_id=organization, queue="ai-batch"
        )
        try:
            assert waiting_ack.wait(timeout=10)
            assert reads == []
            client.hset(key, "resolve_until", 0)
            assert not waiter.done()
        finally:
            permit_ack.set()
        assert waiter.result(timeout=10)
    assert reads == [organization]
    assert event.wait(timeout=10)


def test_real_subscriber_disconnect_recovers_subscription(
    metered_broker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = fairness._client()
    monkeypatch.setattr(fairness, "_SOCKET_TIMEOUT_SECONDS", 10.0)
    original = fairness._signals_for(client)
    key = f"{fairness._BUCKET_KEY_PREFIX}:{uuid4()}:ai-batch"
    event = original.register(key)
    assert client.client_kill_filter(_type="pubsub") == 1
    # A connect callback can precede the final I/O failure. Observe the
    # terminal failure, not that intermediate callback, before starting
    # the next attempt. The failed attempt must retain its outage result.
    assert original.stopped.wait(timeout=10)
    assert event.is_set()
    with pytest.raises(redis.ConnectionError):
        original.check()
    recovered = fairness._signals_for(client)
    assert recovered is not original
    event = recovered.register(key)
    event.clear()
    client.publish(fairness._RESOLVER_CHANNEL, key)
    assert event.wait(timeout=10)
    assert client.pubsub_numsub(fairness._RESOLVER_CHANNEL) == [
        (fairness._RESOLVER_CHANNEL.encode(), 1)
    ]


def test_fork_reset_discards_inherited_registration_and_factory_lock(
    metered_broker: str,
) -> None:
    client = fairness._client()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{uuid4()}:ai-batch"
    old = fairness._signals_for(client)
    old_event = old.register(key)
    inherited_lock = fairness._resolver_registry.lock
    try:
        with inherited_lock:
            fairness._resolver_registry.reset()
            assert fairness._resolver_registry.lock is not inherited_lock
            replacement = fairness._signals_for(client)
            assert replacement is not old
            event = replacement.register(key)
            assert event is not old_event
            event.clear()
    finally:
        old.close()


def test_successor_preserves_waiters_after_lease_expiry(
    metered_broker: str, frozen_clock: dict[str, float]
) -> None:
    client = fairness._client()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{uuid4()}:ai-batch"
    signals = fairness._signals_for(client)
    event = signals.register(key)
    args = (frozen_clock["t"], fairness.MAX_QUOTA_PER_MINUTE / 60.0)
    assert client.eval(fairness._CREDIT_POSSIBLE_LUA, 1, key, *args, "old") == 1
    client.hset(key, "resolve_until", 3_000_000_000)
    assert client.eval(fairness._CREDIT_POSSIBLE_LUA, 1, key, *args, "waiter") == 2
    client.hset(key, "resolve_until", 0)
    assert client.eval(fairness._CREDIT_POSSIBLE_LUA, 1, key, *args, "new") == 1
    assert client.hget(key, "notify") == b"1"
    client.eval(fairness._RELEASE_RESOLVER_LUA, 1, key, "old")
    assert client.hget(key, "resolving") == b"new"
    assert not client.exists(f"{key}:completed")
    assert not event.is_set()
    client.eval(fairness._RELEASE_RESOLVER_LUA, 1, key, "new")
    assert event.wait(timeout=10)


@pytest.mark.parametrize("database_error", [False, True])
def test_failed_authority_releases_and_signals_waiters(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
    database_error: bool,
) -> None:
    organization = uuid4()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization}:ai-batch"
    client = fairness._client()
    signals = fairness._signals_for(client)
    event = signals.register(key)

    def resolve(_organization: UUID) -> object:
        client.hset(key, "resolve_until", 3_000_000_000)
        assert (
            client.eval(
                fairness._CREDIT_POSSIBLE_LUA,
                1,
                key,
                frozen_clock["t"],
                fairness.MAX_QUOTA_PER_MINUTE / 60.0,
                "waiting-worker",
            )
            == 2
        )
        if database_error:
            message = "synthetic authority failure"
            raise DatabaseError(message)
        return []

    monkeypatch.setattr(fairness, "_organization_quotas", resolve)
    if database_error:
        with pytest.raises(DatabaseError, match="synthetic authority failure"):
            fairness.fair_acquire(organization_id=organization, queue="ai-batch")
    else:
        assert not fairness.fair_acquire(organization_id=organization, queue="ai-batch")
    assert client.hget(key, "resolving") is None
    assert event.wait(timeout=10)


def test_timeout_rechecks_abandoned_lease_without_refusal(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    organization = uuid4()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization}:ai-batch"
    client = fairness._client()
    signals = fairness._signals_for(client)
    client.hset(key, mapping={"resolving": "abandoned", "resolve_until": 3_000_000_000})
    waits: list[int] = []
    reads: list[UUID] = []

    def timeout(event: threading.Event) -> None:
        waits.append(fairness._RESOLVER_WAIT_MILLISECONDS)
        client.hset(key, "resolve_until", 0)

    def resolve(org: UUID) -> object:
        reads.append(org)
        return {"ai-batch": 1}

    monkeypatch.setattr(signals, "wait", timeout)
    monkeypatch.setattr(fairness, "_organization_quotas", resolve)
    assert fairness.fair_acquire(organization_id=organization, queue="ai-batch")
    assert waits == [fairness._RESOLVER_WAIT_MILLISECONDS]
    assert reads == [organization]


@pytest.mark.parametrize(
    "scenario",
    [
        ("clinical", False, True),
        ("ai-batch", False, False),
        ("clinical", True, False),
        ("ai-batch", True, False),
    ],
)
def test_notification_outage_preserves_authority_and_failure_order(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
    scenario: tuple[str, bool, bool],
) -> None:
    queue, malformed, allowed = scenario
    organization = uuid4()
    key = f"{fairness._BUCKET_KEY_PREFIX}:{organization}:{queue}"
    client = fairness._client()
    signals = fairness._signals_for(client)
    client.hset(
        key, mapping={"resolving": "other-worker", "resolve_until": 3_000_000_000}
    )
    reads: list[UUID] = []

    def unavailable(*args: object, **kwargs: object) -> object:
        message = "synthetic notification outage"
        raise redis.ConnectionError(message)

    def resolve(org: UUID) -> object:
        reads.append(org)
        return [] if malformed else {}

    monkeypatch.setattr(signals, "wait", unavailable)
    monkeypatch.setattr(fairness, "_organization_quotas", resolve)
    assert fairness.fair_acquire(organization_id=organization, queue=queue) is allowed
    assert reads == [organization]
    assert client.hget(key, "resolving") == b"other-worker"


@pytest.mark.parametrize("quota", [1, 60, 600, fairness.MAX_QUOTA_PER_MINUTE])
def test_merged_hash_reads_preserve_legacy_decisions_and_balances(
    metered_broker: str, frozen_clock: dict[str, float], quota: int
) -> None:
    client = fairness._client()
    prefix = f"{fairness._BUCKET_KEY_PREFIX}:{uuid4()}"
    old, new = f"{prefix}:old", f"{prefix}:new"
    for balance, elapsed in product(
        (-1.0, 0.0, 0.999, 1.0, 99999.0), (-1, 0, 0.0006, 1, 60)
    ):
        for key in (old, new):
            client.delete(key, f"{key}:completed")
            client.hset(key, mapping={"tokens": balance, "touched": frozen_clock["t"]})
        now = frozen_clock["t"] + elapsed
        args = (now, fairness.MAX_QUOTA_PER_MINUTE / 60.0, "equivalence")
        assert client.eval(LEGACY_CREDIT, 1, old, *args) == client.eval(
            fairness._CREDIT_POSSIBLE_LUA, 1, new, *args
        )
        bucket_args = (quota, now, quota / 60.0, "equivalence")
        assert client.eval(LEGACY_BUCKET, 1, old, *bucket_args) == client.eval(
            fairness._BUCKET_LUA, 1, new, *bucket_args
        )
        assert client.hmget(old, "tokens", "touched", "resolving") == client.hmget(
            new, "tokens", "touched", "resolving"
        )
        assert not client.exists(f"{new}:completed")


def test_merged_bucket_reads_remove_commands_without_extra_roundtrips(
    metered_broker: str,
    frozen_clock: dict[str, float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = fairness._client()
    organization = uuid4()
    legacy_key = f"{fairness._BUCKET_KEY_PREFIX}:{uuid4()}:ai-batch"
    for source in (fairness._CREDIT_POSSIBLE_LUA, fairness._BUCKET_LUA):
        client.script_load(source)
    calls: list[str] = []
    original = fairness._evaluate
    monkeypatch.setattr(
        fairness, "_organization_quotas", lambda _org: {"ai-batch": 600}
    )

    def evaluate(
        backend: redis.Redis, source: str, key: str, *args: str | float
    ) -> object:
        calls.append(source)
        return original(backend, source, key, *args)

    monkeypatch.setattr(fairness, "_evaluate", evaluate)
    before = client.info("commandstats").get("cmdstat_hget", {}).get("calls", 0)
    client.eval(LEGACY_BUCKET, 1, legacy_key, 600, frozen_clock["t"], 10, "equivalence")
    after_legacy = client.info("commandstats")["cmdstat_hget"]["calls"]
    assert after_legacy - before == 3
    assert fairness.fair_acquire(organization_id=organization, queue="ai-batch")
    after_new = client.info("commandstats")["cmdstat_hget"]["calls"]
    assert after_new == after_legacy
    assert calls == [fairness._CREDIT_POSSIBLE_LUA, fairness._BUCKET_LUA]
