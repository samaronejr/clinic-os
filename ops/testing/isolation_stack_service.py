"""Validate one stack service and its closed environment/port mapping."""

from __future__ import annotations

import re
from typing import Final, Never

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_service_contracts import validate_service_contracts

SERVICE_KEYS: Final = frozenset(
    {
        "name",
        "image_id",
        "uid",
        "gid",
        "command",
        "start_policy",
        "network_mode",
        "network_refs",
        "extra_hosts",
        "volume_mounts",
        "published_ports",
        "environment_contract",
        "image_contract",
        "filesystem_contract",
    }
)
PORT_KEYS: Final = frozenset({"transport", "host", "port"})
PUBLISHED_PORT_KEYS: Final = PORT_KEYS | {"container_port"}
ENVIRONMENT_KEYS: Final = frozenset({"literal", "secret_keys", "absent_keys"})
ENTRY_KEYS: Final = frozenset({"name", "value"})
NETWORK_REF_KEYS: Final = frozenset({"network_name", "aliases"})
VOLUME_MOUNT_KEYS: Final = frozenset({"volume_name", "target", "read_only"})
NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
IMAGE_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_PORT: Final = 65535
PROTECTED_DATABASE_PORT: Final = 5432


def _fail(message: str) -> Never:
    raise IsolationError(message)


def validate_stack_service(
    service: JsonObject,
    desired_ports: set[tuple[str, str, int]],
) -> str:
    """Require exact service identity, startup, mapping, and environment fields."""
    if set(service) != set(SERVICE_KEYS):
        _fail("stack service has the wrong closed key set")
    name = _service_identity(service)
    _service_start_and_network(service)
    if service["extra_hosts"] != []:
        _fail("service extra-host mappings are not implemented in this slice")
    _volume_mounts(service["volume_mounts"])
    for item in _objects(service["published_ports"], "published ports"):
        if validate_loopback_port(item, published=True) not in desired_ports:
            _fail("service published port is not reserved")
    validate_environment_contract(
        _object(service["environment_contract"], "environment contract")
    )
    validate_service_contracts(
        service["image_contract"],
        service["filesystem_contract"],
    )
    return name


def validate_loopback_port(
    value: JsonObject,
    *,
    published: bool,
) -> tuple[str, str, int]:
    """Return one normalized nonprotected TCP loopback endpoint."""
    expected = PUBLISHED_PORT_KEYS if published else PORT_KEYS
    if set(value) != set(expected):
        _fail("port has the wrong closed key set")
    transport = _text(value["transport"], "port transport")
    host = _text(value["host"], "port host")
    port = value["port"]
    if transport != "tcp" or host not in {"127.0.0.1", "::1"}:
        _fail("port is not a TCP loopback endpoint")
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= MAX_PORT
        or port == PROTECTED_DATABASE_PORT
    ):
        _fail("loopback host port is invalid")
    if published:
        container_port = value["container_port"]
        if (
            isinstance(container_port, bool)
            or not isinstance(container_port, int)
            or not 1 <= container_port <= MAX_PORT
        ):
            _fail("container port is invalid")
    return transport, host, port


def validate_environment_contract(value: JsonObject) -> None:
    """Validate the closed nonsecret environment mapping shared by claims."""
    _environment(value)


def _service_identity(service: JsonObject) -> str:
    name = _text(service["name"], "service name")
    if NAME_PATTERN.fullmatch(name) is None:
        _fail("service name is invalid")
    image = _text(service["image_id"], "service image ID")
    if IMAGE_PATTERN.fullmatch(image) is None:
        _fail("service image ID is invalid")
    for field in ("uid", "gid"):
        value = service[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail(f"service {field} is invalid")
    command = _strings(service["command"], "service command")
    if not command or any(not item for item in command):
        _fail("service command is empty")
    return name


def _service_start_and_network(service: JsonObject) -> None:
    policy = service["start_policy"]
    if policy not in {"running-before-activation", "prepared-attest-before-use"}:
        _fail("service start policy is invalid")
    mode = service["network_mode"]
    refs = _objects(service["network_refs"], "service network refs")
    if (mode == "bridge") != bool(refs) or mode not in {"bridge", "host", "none"}:
        _fail("service network mode and references disagree")
    names: list[str] = []
    for item in refs:
        if set(item) != set(NETWORK_REF_KEYS):
            _fail("service network reference has the wrong closed key set")
        names.append(_text(item["network_name"], "service network name"))
        aliases = _strings(item["aliases"], "service network aliases")
        if not aliases or aliases != sorted(set(aliases)):
            _fail("service network aliases are not sorted and unique")
    if names != sorted(set(names)):
        _fail("service network references are not sorted and unique")


def _volume_mounts(value: JsonValue) -> None:
    mounts = _objects(value, "service volume mounts")
    identities: list[tuple[str, str]] = []
    for item in mounts:
        if set(item) != set(VOLUME_MOUNT_KEYS):
            _fail("service volume mount has the wrong closed key set")
        volume_name = _text(item["volume_name"], "service volume name")
        target = _text(item["target"], "service volume target")
        if not target.startswith("/") or not isinstance(item["read_only"], bool):
            _fail("service volume mount is invalid")
        identities.append((target, volume_name))
    if identities != sorted(set(identities)):
        _fail("service volume mounts are not sorted and unique")


def _environment(value: JsonObject) -> None:
    if set(value) != set(ENVIRONMENT_KEYS):
        _fail("environment contract has the wrong closed key set")
    literal = _objects(value["literal"], "literal environment")
    literal_names: list[str] = []
    for item in literal:
        if set(item) != set(ENTRY_KEYS):
            _fail("literal environment entry has the wrong key set")
        literal_names.append(_text(item["name"], "environment name"))
        _ = _text(item["value"], "environment value")
    secret = _strings(value["secret_keys"], "secret key names")
    absent = _strings(value["absent_keys"], "absent key names")
    for values, context in (
        (literal_names, "literal"),
        (secret, "secret"),
        (absent, "absent"),
    ):
        if values != sorted(set(values)):
            _fail(f"{context} environment names are not sorted and unique")
    if set(literal_names) & set(secret) or set(literal_names + secret) & set(absent):
        _fail("environment contract key sets overlap")


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
