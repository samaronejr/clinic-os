"""Inert contracts for the real broker gate's event reader."""

import pytest
from redis import Redis
from redis.connection import Connection

from renewal.broker_support import broker, read_event

__all__ = ("broker",)


@pytest.fixture
def broker_url() -> str:
    """Supply an address without starting Redis."""
    return "redis://127.0.0.1:1/0"


def test_event_reader_socket_timeout_outlasts_blpop(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    def blpop(client: Redis, key: str, *, timeout: int) -> list[str]:
        # Construct the connection without opening a socket, so defaults count.
        connection = client.connection_pool.make_connection()
        assert isinstance(connection, Connection)
        socket_timeout = connection.socket_timeout
        assert socket_timeout is not None
        assert socket_timeout > timeout
        return [key, "[]"]

    monkeypatch.setattr(Redis, "flushdb", lambda _client: True)
    monkeypatch.setattr(Redis, "blpop", blpop)
    client = request.getfixturevalue("broker")
    assert isinstance(client, Redis)
    assert read_event(client, "completed:synthetic") == []
