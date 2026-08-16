"""Attest one Docker service against its reserved stack mapping."""

from __future__ import annotations

import copy
from typing import Never

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue


def observe_live_service(
    desired: JsonObject,
    current: JsonObject,
    *,
    strict: bool,
) -> JsonObject:
    """Return the exact observed service after every live mapping matches."""
    _validate_identity(desired, current, strict=strict)
    mounts = _validated_mounts(desired, current, strict=strict)
    attachments = _validated_attachments(desired, current, strict=strict)
    _validate_network_mode(desired, current, attachments)
    state = current.get("state")
    if state != "running":
        _fail("live stack service is not running")
    return {
        "command": copy.deepcopy(desired.get("command")),
        "container_id": current.get("id"),
        "environment_contract": copy.deepcopy(desired.get("environment_contract")),
        "extra_hosts": copy.deepcopy(desired.get("extra_hosts")),
        "filesystem_contract": copy.deepcopy(desired.get("filesystem_contract")),
        "gid": desired.get("gid"),
        "image_contract": copy.deepcopy(desired.get("image_contract")),
        "image_id": desired.get("image_id"),
        "name": desired.get("name"),
        "network_attachments": _json_objects(attachments),
        "network_mode": desired.get("network_mode"),
        "published_ports": copy.deepcopy(desired.get("published_ports")),
        "state": state,
        "uid": desired.get("uid"),
        "volume_mounts": _json_objects(mounts),
    }


def _validate_identity(
    desired: JsonObject,
    current: JsonObject,
    *,
    strict: bool,
) -> None:
    expected_user = {
        str(desired.get("uid")),
        f"{desired.get('uid')}:{desired.get('gid')}",
    }
    if current.get("config_user") == "" and desired.get("uid") == 0:
        expected_user.add("")
    if current.get("config_user") not in expected_user:
        _fail("live stack service user identity drifted")
    fields = ["image_id", "published_ports"]
    if strict or "command" in current:
        fields.append("command")
    for field in fields:
        if current.get(field) != desired.get(field):
            _fail(f"live stack service {field} drifted")


def _validated_mounts(
    desired: JsonObject,
    current: JsonObject,
    *,
    strict: bool,
) -> list[JsonObject]:
    expected = _objects(desired.get("volume_mounts"), "service mounts")
    raw = current.get("mounts")
    if raw is None and not strict:
        return copy.deepcopy(expected)
    actual = [
        {
            "read_only": item.get("read_only"),
            "target": item.get("target"),
            "volume_name": item.get("source"),
        }
        for item in _objects(raw, "live service mounts")
    ]
    if actual != expected:
        _fail("live stack service volume mounts drifted")
    return actual


def _validated_attachments(
    desired: JsonObject,
    current: JsonObject,
    *,
    strict: bool,
) -> list[JsonObject]:
    expected = _objects(desired.get("network_refs"), "service network refs")
    raw = current.get("network_attachments")
    actual = [] if raw is None and not strict else _objects(raw, "network attachments")
    projected = [
        {"aliases": item.get("aliases"), "network_name": item.get("network_name")}
        for item in actual
    ]
    if projected != expected:
        _fail("live stack service network attachments drifted")
    return actual


def _validate_network_mode(
    desired: JsonObject,
    current: JsonObject,
    attachments: list[JsonObject],
) -> None:
    actual = current.get("network_mode")
    if not isinstance(actual, str):
        _fail("live stack service network mode is not textual")
    if desired.get("network_mode") != "bridge":
        if actual != desired.get("network_mode"):
            _fail("live stack service network mode drifted")
        return
    names = {
        _text(item.get("network_name"), "network attachment name")
        for item in attachments
    }
    if actual not in names | {"bridge", "default"}:
        _fail("live stack service network mode drifted")


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail(f"{context} must be an object array")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict):
            _fail(f"{context} must be an object array")
        result.append(item)
    return result


def _json_objects(value: list[JsonObject]) -> list[JsonValue]:
    return [copy.deepcopy(item) for item in value]


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be textual")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
