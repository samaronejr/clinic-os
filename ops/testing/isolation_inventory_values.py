"""Decode closed scalar, entry, and published-port inventory values."""

from __future__ import annotations

from typing import Final, Never, cast

import rfc8785

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

ENTRY_KEYS: Final = frozenset({"name", "value"})
PORT_KEYS: Final = frozenset({"transport", "host", "port", "container_port"})


def _fail(message: str) -> Never:
    raise IsolationError(message)


def entries(value: object, context: str) -> list[JsonValue]:
    """Decode one sorted unique Docker label or option array."""
    normalized: list[JsonValue] = []
    for raw in array(value, context):
        item = closed_object(raw, ENTRY_KEYS, context)
        normalized.append(
            {"name": text(item["name"], context), "value": text(item["value"], context)}
        )
    result = sorted(normalized, key=rfc8785.dumps)
    if len(result) != len({rfc8785.dumps(item) for item in result}):
        _fail(f"{context} must be unique")
    return result


def ports(value: object) -> list[JsonValue]:
    """Decode one deterministically sorted published-port array."""
    normalized: list[JsonValue] = []
    for raw in array(value, "published ports"):
        item = closed_object(raw, PORT_KEYS, "published port")
        normalized.append(
            {
                "container_port": integer(
                    item["container_port"], "container port", minimum=1, maximum=65535
                ),
                "host": text(item["host"], "published host"),
                "port": integer(
                    item["port"], "published port", minimum=1, maximum=65535
                ),
                "transport": text(item["transport"], "published transport"),
            }
        )
    return sorted(normalized, key=_port_key)


def closed_object(
    value: object, keys: frozenset[str], context: str
) -> dict[str, object]:
    """Decode an object whose key set must match exactly."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{context} must be an object")
    item = {str(key): entry for key, entry in value.items()}
    if item.keys() != keys:
        _fail(f"{context} has unknown or missing fields")
    return item


def array(value: object, context: str) -> list[object]:
    """Decode a JSON array without weakening its element type."""
    if not isinstance(value, list):
        _fail(f"{context} must be an array")
    return cast("list[object]", value)


def string_array(value: object, context: str) -> list[str]:
    """Decode an array containing only strings."""
    return [text(item, context) for item in array(value, context)]


def text(value: object, context: str) -> str:
    """Decode one required string value."""
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def integer(
    value: object, context: str, *, minimum: int = 0, maximum: int | None = None
) -> int:
    """Decode one bounded integer while excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{context} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        _fail(f"{context} is out of range")
    return value


def json_array(items: list[JsonObject]) -> list[JsonValue]:
    """Widen a validated object list to the recursive JSON array type."""
    return list(items)


def _port_key(item: JsonValue) -> tuple[str, str, int, int]:
    port = cast("JsonObject", item)
    return (
        text(port["transport"], "transport"),
        text(port["host"], "host"),
        integer(port["port"], "port"),
        integer(port["container_port"], "container port"),
    )
