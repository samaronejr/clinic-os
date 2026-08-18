"""Validate dependency-owned volume and network observation identities."""

from __future__ import annotations

import copy
import re
from typing import Final, Never

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

SHA256: Final = re.compile(r"[0-9a-f]{64}")
UUID: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
NETWORK_KEYS: Final = frozenset(
    {
        "access",
        "attachable",
        "driver",
        "internal",
        "labels",
        "network_id",
        "network_name",
        "owner_claim_id",
    }
)
VOLUME_KEYS: Final = frozenset(
    {
        "access",
        "created_at",
        "driver",
        "labels",
        "mountpoint",
        "options",
        "owner_claim_id",
        "scope",
        "volume_name",
    }
)


def borrowed_resource_names(value: JsonValue, key: str, access: str) -> set[str]:
    """Validate authored borrowed references and return their unique names."""
    items = _objects(value, f"borrowed {key}")
    if access not in {"read-only", "attach"}:
        _fail("internal borrowed access contract drifted")
    identities: list[tuple[str, str]] = []
    for item in items:
        owner = item.get("owner_claim_id")
        name = item.get(key)
        if (
            set(item) != {"owner_claim_id", key, "access"}
            or not isinstance(owner, str)
            or UUID.fullmatch(owner) is None
            or not isinstance(name, str)
            or not name
            or item.get("access") != access
        ):
            _fail(f"borrowed {key} has the wrong closed contract")
        identities.append((name, owner))
    if identities != sorted(set(identities)):
        _fail(f"borrowed {key} references are not sorted and unique")
    return {name for name, _ in identities}


def validate_borrowed_resources(desired: JsonObject, observed: JsonObject) -> None:
    """Require each borrowed observation to retain its owner and live identity."""
    _validate_networks(
        _objects(desired.get("borrowed_network_refs"), "borrowed network refs"),
        _objects(observed.get("borrowed_networks"), "borrowed networks"),
    )
    _validate_volumes(
        _objects(desired.get("borrowed_volume_refs"), "borrowed volume refs"),
        _objects(observed.get("borrowed_volumes"), "borrowed volumes"),
    )


def observe_borrowed_resources(
    desired: JsonObject,
    inventory: JsonObject,
) -> tuple[list[JsonObject], list[JsonObject]]:
    """Project exact live identities for every dependency-owned resource."""
    volumes = _objects(inventory.get("volumes"), "live volumes")
    networks = _objects(inventory.get("networks"), "live networks")
    observed_volumes: list[JsonObject] = []
    observed_networks: list[JsonObject] = []
    for reference in _objects(
        desired.get("borrowed_volume_refs"), "borrowed volume refs"
    ):
        name = reference.get("volume_name")
        matches = [item for item in volumes if item.get("volume_name") == name]
        if len(matches) != 1:
            _fail("borrowed volume is missing or ambiguous")
        recorded = copy.deepcopy(matches[0])
        recorded["access"] = "read-only"
        recorded["owner_claim_id"] = reference.get("owner_claim_id")
        observed_volumes.append(recorded)
    for reference in _objects(
        desired.get("borrowed_network_refs"), "borrowed network refs"
    ):
        name = reference.get("network_name")
        matches = [item for item in networks if item.get("name") == name]
        if len(matches) != 1:
            _fail("borrowed network is missing or ambiguous")
        current = matches[0]
        observed_networks.append(
            {
                "access": "attach",
                "attachable": current.get("attachable"),
                "driver": current.get("driver"),
                "internal": current.get("internal"),
                "labels": copy.deepcopy(current.get("labels")),
                "network_id": current.get("id"),
                "network_name": name,
                "owner_claim_id": reference.get("owner_claim_id"),
            }
        )
    observed_volumes.sort(key=lambda item: str(item.get("volume_name")))
    observed_networks.sort(key=lambda item: str(item.get("network_name")))
    projected: JsonObject = {
        "borrowed_networks": _json_values(observed_networks),
        "borrowed_volumes": _json_values(observed_volumes),
    }
    validate_borrowed_resources(desired, projected)
    return observed_volumes, observed_networks


def _validate_networks(refs: list[JsonObject], actual: list[JsonObject]) -> None:
    if len(refs) != len(actual):
        _fail("borrowed network observation is incomplete")
    for reference, observed in zip(refs, actual, strict=True):
        if (
            set(observed) != NETWORK_KEYS
            or observed.get("owner_claim_id") != reference.get("owner_claim_id")
            or observed.get("network_name") != reference.get("network_name")
            or observed.get("access") != "attach"
            or observed.get("driver") != "bridge"
            or not isinstance(observed.get("attachable"), bool)
            or not isinstance(observed.get("internal"), bool)
            or not _sha(observed.get("network_id"))
            or not isinstance(observed.get("labels"), list)
        ):
            _fail("borrowed network identity drifted")


def _validate_volumes(refs: list[JsonObject], actual: list[JsonObject]) -> None:
    if len(refs) != len(actual):
        _fail("borrowed volume observation is incomplete")
    for reference, observed in zip(refs, actual, strict=True):
        textual = ("created_at", "driver", "mountpoint", "scope", "volume_name")
        if (
            set(observed) != VOLUME_KEYS
            or observed.get("owner_claim_id") != reference.get("owner_claim_id")
            or observed.get("volume_name") != reference.get("volume_name")
            or observed.get("access") != "read-only"
            or any(not _text(observed.get(field)) for field in textual)
            or not isinstance(observed.get("labels"), list)
            or not isinstance(observed.get("options"), list)
        ):
            _fail("borrowed volume identity drifted")


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail(f"{context} must be an object array")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail(f"{context} must be an object array")
        result.append(item)
    return result


def _json_values(objects: list[JsonObject]) -> list[JsonValue]:
    values: list[JsonValue] = []
    values.extend(objects)
    return values


def _sha(value: JsonValue) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _text(value: JsonValue) -> bool:
    return isinstance(value, str) and bool(value)


def _fail(message: str) -> Never:
    raise IsolationError(message)
