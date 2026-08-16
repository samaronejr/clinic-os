"""Derive the closed browser-runner Docker create command."""

from __future__ import annotations

import hashlib
from typing import Final, Never, cast

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
)

INTENT_LABEL_PLACEHOLDER: Final = "<intent-sha256>"


def runner_create_argv_sha(
    ledger: JsonObject,
    claim: JsonObject,
    service: JsonObject,
    container_name: str,
) -> str:
    """Hash the exact create argv with an intent-label cycle breaker."""
    arguments = runner_create_arguments(
        ledger,
        claim,
        service,
        container_name,
        INTENT_LABEL_PLACEHOLDER,
    )
    raw = b"docker\0" + b"".join(item.encode() + b"\0" for item in arguments)
    return hashlib.sha256(raw).hexdigest()


def runner_create_arguments(
    ledger: JsonObject,
    claim: JsonObject,
    service: JsonObject,
    container_name: str,
    intent_sha256: str,
) -> tuple[str, ...]:
    """Return the only Docker create tuple authorized by runner intent."""
    arguments = ["create", "--rm", "--interactive", "--name", container_name]
    labels = (
        f"clinic.phase1a.attempt={ledger['attempt_id']}",
        f"clinic.phase1a.claim={claim['claim_id']}",
        f"clinic.phase1a.service={service['name']}",
        f"clinic.phase1a.runner-create-intent={intent_sha256}",
    )
    for label in labels:
        arguments.extend(("--label", label))
    arguments.extend(("--user", f"{service['uid']}:{service['gid']}"))
    _append_network(arguments, service)
    _append_environment(arguments, service)
    _append_extra_hosts(arguments, service)
    _append_volume_mounts(arguments, service)
    _append_filesystem_contract(arguments, service)
    if service.get("published_ports") != []:
        _fail("prepared runner cannot publish a port before attestation")
    arguments.append(_text(service.get("image_id"), "runner image ID"))
    command = _strings(service.get("command"), "runner command")
    if not command:
        _fail("runner command is empty")
    arguments.extend(command)
    return tuple(arguments)


def _append_network(arguments: list[str], service: JsonObject) -> None:
    mode = _text(service.get("network_mode"), "runner network mode")
    references = _objects(service.get("network_refs"), "runner network refs")
    if references:
        _fail("prepared runner network references are not implemented")
    arguments.extend(("--network", mode))


def _append_environment(arguments: list[str], service: JsonObject) -> None:
    contract = _object(service.get("environment_contract"), "runner environment")
    for item in _objects(contract.get("literal"), "literal environment"):
        name = _text(item.get("name"), "environment name")
        value = _text(item.get("value"), "environment value")
        arguments.extend(("--env", f"{name}={value}"))


def _append_extra_hosts(arguments: list[str], service: JsonObject) -> None:
    for item in _objects(service.get("extra_hosts"), "runner extra hosts"):
        hostname = _text(item.get("hostname"), "extra-host name")
        address = _text(item.get("address"), "extra-host address")
        arguments.extend(("--add-host", f"{hostname}:{address}"))


def _append_volume_mounts(arguments: list[str], service: JsonObject) -> None:
    for item in _objects(service.get("volume_mounts"), "runner volume mounts"):
        source = _text(item.get("volume_name"), "volume name")
        target = _text(item.get("target"), "volume target")
        read_only = item.get("read_only") is True
        value = f"type=volume,src={source},dst={target}"
        if read_only:
            value += ",readonly"
        arguments.extend(("--mount", value))


def _append_filesystem_contract(
    arguments: list[str],
    service: JsonObject,
) -> None:
    value = service.get("filesystem_contract")
    if value is None:
        return
    contract = _object(value, "runner filesystem contract")
    if contract.get("root_read_only") is True:
        arguments.append("--read-only")
    targets: list[str] = []
    for item in _objects(contract.get("tmpfs_mounts"), "runner tmpfs mounts"):
        target = _text(item.get("target"), "tmpfs target")
        targets.append(target)
        arguments.extend(("--tmpfs", f"{target}:{runner_tmpfs_options(item)}"))
    if _strings(contract.get("writable_paths"), "runner writable paths") != targets:
        _fail("runner writable paths must equal its sorted tmpfs targets")
    arguments.extend(("--ipc", _text(contract.get("ipc_mode"), "IPC mode")))
    shm_size = _nonnegative(contract.get("shm_size_bytes"), "shared memory size")
    arguments.extend(("--shm-size", str(shm_size)))


def runner_tmpfs_options(item: JsonObject) -> str:
    """Serialize one closed tmpfs contract for create and reinspection."""
    options = [
        "rw",
        f"size={_positive(item.get('size_bytes'), 'tmpfs size')}",
        f"mode={_nonnegative(item.get('mode'), 'tmpfs mode'):o}",
        f"uid={_nonnegative(item.get('uid'), 'tmpfs uid')}",
        f"gid={_nonnegative(item.get('gid'), 'tmpfs gid')}",
    ]
    options.extend(
        name for name in ("nosuid", "nodev", "noexec") if item.get(name) is True
    )
    return ",".join(options)


def _positive(value: JsonValue, context: str) -> int:
    result = _nonnegative(value, context)
    if result == 0:
        _fail(f"{context} must be positive")
    return result


def _nonnegative(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{context} must be a nonnegative integer")
    return value


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} must be a string array")
    return cast("list[str]", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{context} must be a nonempty string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
