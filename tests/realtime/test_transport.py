"""PHI-free transport and commit-only publication contract."""

from __future__ import annotations

import json
import subprocess
import sys
from multiprocessing import get_context
from os import environ, fstat, getpid
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.realtime import transport
from apps.realtime.stream import _event
from apps.realtime.transport import event_bytes, topic_hash
from django.conf import settings as django_settings
from django.db.backends.base.base import BaseDatabaseWrapper
from redis import Redis

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper
    from redis import ConnectionPool
    from redis.client import PubSub

PublisherIdentity = tuple[int, int]


def test_event_schema_is_closed_and_topic_is_opaque(settings: SettingsWrapper) -> None:
    settings.REALTIME_TOPIC_SECRET = uuid4().hex
    topic = f"clinic:{uuid4()}:agenda"
    payload = json.loads(event_bytes(topic, "agenda", 1))
    assert payload == {"topic_hash": topic_hash(topic), "kind": "agenda", "version": 1}
    assert len(payload["topic_hash"]) == 64
    assert topic not in json.dumps(payload)


@pytest.mark.parametrize(
    ("kind", "version"),
    [
        ("SINTETICO-SENTINELA-PHI", 1),
        ("agenda\ndata: forged", 1),
        ("agenda", True),
        ("agenda", -1),
    ],
)
def test_unregistered_kind_or_invalid_version_cannot_carry_text(
    kind: str, version: int
) -> None:
    with pytest.raises(ValueError, match="invalid realtime event"):
        event_bytes(f"clinic:{uuid4()}:agenda", kind, version)


@pytest.mark.parametrize(
    "change",
    [
        {"text": "SINTETICO-SENTINELA-PHI"},
        {"kind": "SINTETICO-SENTINELA-PHI"},
        {"kind": []},
        {"topic_hash": "SINTETICO-SENTINELA-PHI"},
        {"topic_hash": []},
        {"version": True},
        {"version": -1},
        {"version": 2**53},
    ],
)
def test_broker_boundary_rejects_extra_fields_and_forged_metadata(
    change: dict[str, object],
) -> None:
    topic = f"clinic:{uuid4()}:agenda"
    value = json.loads(event_bytes(topic, "agenda", 1))
    value.update(change)
    assert _event(json.dumps(value).encode(), frozenset({topic_hash(topic)})) is None


def test_publisher_pool_is_scoped_to_broker_url_and_process(
    monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> None:
    pools: list[ConnectionPool] = []

    def publish(client: Redis, channel: str, message: bytes) -> int:
        pools.append(client.connection_pool)
        return 1

    monkeypatch.setattr(Redis, "publish", publish)
    monkeypatch.setattr(transport, "getpid", lambda: 10)
    settings.REALTIME_REDIS_URL = "redis://127.0.0.1:1/0"
    topic = f"clinic:{uuid4()}:inbox"
    transport.publish(topic=topic, kind="inbox", version=1)
    transport.publish(topic=topic, kind="inbox", version=2)
    settings.REALTIME_REDIS_URL = "redis://127.0.0.1:1/1"
    transport.publish(topic=topic, kind="inbox", version=3)
    monkeypatch.setattr(transport, "getpid", lambda: 11)
    transport.publish(topic=topic, kind="inbox", version=4)

    first, repeat, other_url, child = pools
    assert first is repeat
    assert len({first, other_url, child}) == 3
    assert [pool.connection_kwargs["db"] for pool in pools] == [0, 0, 1, 1]
    with transport.redis_client() as client:
        assert client.connection_pool not in pools


def _socket_identity(fd: int) -> PublisherIdentity | None:
    try:
        stat = fstat(fd)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def _publisher_socket() -> tuple[int, PublisherIdentity]:
    pool = transport._publisher_pool(django_settings.REALTIME_REDIS_URL, getpid())
    conn = pool.get_connection()
    try:
        sock = conn._get_socket()
        assert sock is not None
        fd = sock.fileno()
        identity = _socket_identity(fd)
        assert identity is not None
    finally:
        pool.release(conn)
    return fd, identity


def _expect_fork_frame(receiver: PubSub, version: int) -> None:
    frame = receiver.get_message(timeout=3)
    assert frame is not None
    assert frame["type"] == "message"
    assert json.loads(frame["data"])["version"] == version


def _prove_publisher_fork() -> None:
    """Observe actual publisher sockets and broker frames across a real fork."""

    def no_database(_wrapper: BaseDatabaseWrapper) -> None:
        message = "fork publisher regression must not connect to PostgreSQL"
        raise AssertionError(message)

    topic = f"clinic:{uuid4()}:inbox"
    with (
        pytest.MonkeyPatch.context() as patch,
        transport.redis_client() as redis,
        redis.pubsub() as receiver,
    ):
        patch.setattr(BaseDatabaseWrapper, "ensure_connection", no_database)
        receiver.subscribe("rt:" + topic_hash(topic))
        ack = receiver.get_message(timeout=3)
        assert ack is not None
        assert ack["type"] == "subscribe"
        transport.publish(topic=topic, kind="inbox", version=1)
        _expect_fork_frame(receiver, 1)
        parent_fd, parent_identity = _publisher_socket()
        fork = get_context("fork")
        read_result, send_result = fork.Pipe(duplex=False)

        def exercise_child() -> None:
            with send_result:
                inherited_before = _socket_identity(parent_fd) == parent_identity
                transport.publish(topic=topic, kind="inbox", version=2)
                _, child_identity = _publisher_socket()
                send_result.send(
                    (
                        inherited_before,
                        _socket_identity(parent_fd) == parent_identity,
                        child_identity != parent_identity,
                    )
                )

        child = fork.Process(target=exercise_child)
        try:
            child.start()
            send_result.close()
            assert read_result.poll(5), "forked publisher did not report its sockets"
            observed = read_result.recv()
            assert observed == (False, False, True), (
                "inherited before publish, inherited after publish, used new socket: "
                f"{observed}"
            )
            _expect_fork_frame(receiver, 2)
            child.join(timeout=5)
            assert child.exitcode == 0
        finally:
            read_result.close()
            send_result.close()
            if child.is_alive():
                child.kill()
                child.join()
            child.close()

        transport.publish(topic=topic, kind="inbox", version=3)
        _expect_fork_frame(receiver, 3)
        assert _publisher_socket() == (parent_fd, parent_identity)


def test_forked_publisher_closes_inherited_socket(redis_url: str) -> None:
    # Fork from a clean interpreter, never from pytest workers' background threads.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import django; django.setup(); "
            "from realtime.test_transport import _prove_publisher_fork; "
            "_prove_publisher_fork()",
        ],
        env={
            **environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "REALTIME_REDIS_URL": redis_url,
            "PYTHONPATH": "tests",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
