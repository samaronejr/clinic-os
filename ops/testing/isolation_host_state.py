"""Compare live host metadata with baseline and recorded task resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

if TYPE_CHECKING:
    from collections.abc import Callable

type ResourceKey = str | tuple[str, str, int]


@dataclass(frozen=True, slots=True)
class _Category:
    label: str
    key: Callable[[JsonObject], ResourceKey]
    task_keys: set[ResourceKey]
    exact_task: dict[ResourceKey, JsonObject] | None = None


def _fail(message: str) -> Never:
    raise IsolationError(message)


def require_known_host_state(ledger: JsonObject, current: JsonObject) -> None:
    """Reject baseline drift, missing task identities, and foreign resources."""
    if set(current) != {"containers", "listeners", "networks", "volumes"}:
        _fail("foreign host inventory has the wrong closed root")
    baseline = _object(ledger.get("baseline"), "ledger baseline")
    claims = _objects(ledger.get("claims"), "ledger claims")
    _compare_category(
        _Category(
            "container",
            lambda item: _text(item.get("id"), "container ID"),
            _task_container_ids(claims),
        ),
        baseline.get("containers"),
        current.get("containers"),
    )
    _compare_category(
        _Category(
            "volume",
            lambda item: _text(item.get("volume_name"), "volume name"),
            _task_resource_keys(claims, "owned_volumes", "volume_name"),
        ),
        baseline.get("volumes"),
        current.get("volumes"),
    )
    _compare_category(
        _Category(
            "network",
            lambda item: _text(item.get("name"), "network name"),
            _task_resource_keys(claims, "owned_networks", "network_name"),
        ),
        baseline.get("networks"),
        current.get("networks"),
    )
    expected_listeners = _task_listeners(claims)
    _compare_category(
        _Category(
            "listener",
            _listener_key,
            set(expected_listeners),
            expected_listeners,
        ),
        baseline.get("listeners"),
        current.get("listeners"),
    )


def _compare_category(
    category: _Category,
    baseline_value: JsonValue,
    current_value: JsonValue,
) -> None:
    label = category.label
    baseline = _keyed(
        _objects(baseline_value, f"baseline {label}s"),
        category.key,
        label,
    )
    current = _keyed(
        _objects(current_value, f"current {label}s"),
        category.key,
        label,
    )
    for identity, expected in baseline.items():
        if current.get(identity) != expected:
            _fail(f"baseline {label} drifted")
    foreign = set(current) - set(baseline) - category.task_keys
    if foreign:
        _fail(f"foreign {label} appeared outside ledger authority")
    missing = category.task_keys - set(current)
    if missing:
        _fail(f"recorded task {label} is missing")
    if category.exact_task is not None and any(
        current[identity] != expected
        for identity, expected in category.exact_task.items()
    ):
        _fail(f"recorded task {label} identity drifted")


def _task_container_ids(claims: list[JsonObject]) -> set[ResourceKey]:
    identifiers: set[ResourceKey] = set()
    for claim in claims:
        if claim.get("status") not in {"prepared", "active"}:
            continue
        observed = _object(claim.get("observed"), "claim observation")
        values = observed.get("container_ids")
        if values is None:
            continue
        identifiers.update(_strings(values, "container IDs"))
    return identifiers


def _task_resource_keys(
    claims: list[JsonObject],
    observed_field: str,
    identity_field: str,
) -> set[ResourceKey]:
    identities: set[ResourceKey] = set()
    for claim in claims:
        if claim.get("status") not in {"prepared", "active"}:
            continue
        observed = _object(claim.get("observed"), "claim observation")
        value = observed.get(observed_field)
        if value is None:
            continue
        identities.update(
            _text(item.get(identity_field), identity_field)
            for item in _objects(value, observed_field)
        )
    return identities


def _task_listeners(claims: list[JsonObject]) -> dict[ResourceKey, JsonObject]:
    result: dict[ResourceKey, JsonObject] = {}
    for claim in claims:
        if claim.get("status") not in {"prepared", "active"}:
            continue
        observed = _object(claim.get("observed"), "claim observation")
        value = observed.get("listeners")
        if value is None:
            continue
        for listener in _objects(value, "claim listeners"):
            identity = _listener_key(listener)
            if identity in result:
                _fail("duplicate task listener identity")
            result[identity] = listener
    return result


def _listener_key(item: JsonObject) -> ResourceKey:
    return (
        _text(item.get("transport"), "listener transport"),
        _text(item.get("host"), "listener host"),
        _integer(item.get("port"), "listener port"),
    )


def _keyed(
    values: list[JsonObject],
    key: Callable[[JsonObject], ResourceKey],
    context: str,
) -> dict[ResourceKey, JsonObject]:
    result: dict[ResourceKey, JsonObject] = {}
    for item in values:
        identity = key(item)
        if identity in result:
            _fail(f"duplicate {context} identity")
        result[identity] = item
    return result


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail(f"{context} must be an object array")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail(f"{context} must be an object array")
        result.append(item)
    return result


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list):
        _fail(f"{context} must be a string array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _fail(f"{context} must be a string array")
        result.append(item)
    return result


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{context} must be an integer")
    return value
