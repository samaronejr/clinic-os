"""Validate closed listener ownership before same-boot comparison."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

if TYPE_CHECKING:
    from collections.abc import Callable

type ListenerKey = tuple[str, str, int]
SHA256_HEX_LENGTH: Final = 64
LISTENER_KEYS: Final = frozenset(
    {
        "argv_sha256",
        "container_id",
        "executable_realpath",
        "host",
        "owner_kind",
        "pid",
        "port",
        "process_start_ticks",
        "socket_inode",
        "transport",
    }
)


def validated_listener_map(
    value: JsonValue,
    context: str,
    key: Callable[[JsonObject], ListenerKey],
) -> dict[ListenerKey, JsonObject]:
    """Return endpoint-keyed rows only after exact owner validation."""
    result: dict[ListenerKey, JsonObject] = {}
    for listener in _objects(value, context):
        _ = listener_owner_kind(listener)
        identity = key(listener)
        if identity in result:
            _fail("duplicate listener identity")
        result[identity] = listener
    return result


def listener_owner_kind(item: JsonObject) -> str:
    """Return one closed readable owner kind or fail on ambiguous provenance."""
    if set(item) != LISTENER_KEYS:
        _fail("listener has the wrong closed key set")
    _ = _positive_integer(item.get("socket_inode"), "listener socket inode")
    owner_kind = _text(item.get("owner_kind"), "listener owner kind")
    if owner_kind == "container":
        if not _text(item.get("container_id"), "listener container ID") or any(
            item.get(field) is not None
            for field in (
                "argv_sha256",
                "executable_realpath",
                "pid",
                "process_start_ticks",
            )
        ):
            _fail("container listener ownership is ambiguous")
        return owner_kind
    if owner_kind != "process" or item.get("container_id") is not None:
        _fail("listener ownership is ambiguous")
    _ = _positive_integer(item.get("pid"), "listener PID")
    _ = _positive_integer(item.get("process_start_ticks"), "listener start ticks")
    if not _text(item.get("executable_realpath"), "listener executable realpath"):
        _fail("process listener executable is unreadable")
    digest = _text(item.get("argv_sha256"), "listener argv SHA-256")
    if len(digest) != SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        _fail("process listener argv SHA-256 is invalid")
    return owner_kind


def task_listener_keys(
    claims: list[JsonObject],
    key: Callable[[JsonObject], ListenerKey],
) -> set[ListenerKey]:
    """Return every desired and observed endpoint under task authority."""
    result: set[ListenerKey] = set()
    for claim in claims:
        if claim.get("status") not in {"reserved", "prepared", "active"}:
            continue
        desired = claim.get("desired")
        if isinstance(desired, dict):
            for field in ("host_ports", "loopback_ports"):
                value = desired.get(field)
                if value is not None:
                    result.update(key(item) for item in _objects(value, field))
        observed = claim.get("observed")
        if isinstance(observed, dict) and observed.get("listeners") is not None:
            result.update(
                key(item)
                for item in _objects(observed.get("listeners"), "claim listeners")
            )
    return result


def task_listeners(
    claims: list[JsonObject],
    key: Callable[[JsonObject], ListenerKey],
) -> dict[ListenerKey, JsonObject]:
    """Return exact prepared and active listener observations by endpoint."""
    result: dict[ListenerKey, JsonObject] = {}
    for claim in claims:
        if claim.get("status") not in {"prepared", "active"}:
            continue
        observed = _object(claim.get("observed"), "claim observation")
        value = observed.get("listeners")
        if value is None:
            continue
        for listener in _objects(value, "claim listeners"):
            identity = key(listener)
            if identity in result:
                _fail("duplicate task listener identity")
            result[identity] = listener
    return result


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail(f"{context} must be an object array")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail(f"{context} must be an object array")
        result.append(item)
    return result


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _positive_integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(f"{context} must be a positive integer")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
