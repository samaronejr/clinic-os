"""Authenticate runner selectors and immutable Docker configuration."""

from __future__ import annotations

from typing import Never, cast

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
)
from ops.testing.isolation_runner_create import runner_tmpfs_options
from ops.testing.isolation_runner_docker_inspection import (
    RunnerContainer,
    RunnerMount,
)
from ops.testing.isolation_stack_observation import validate_stack_observation

DEFAULT_SHM_SIZE = 64 * 1024 * 1024


def runner_labels(
    ledger: JsonObject,
    claim: JsonObject,
    creation: JsonObject,
) -> dict[str, str]:
    """Return the exact four-label runner ownership boundary."""
    return {
        "clinic.phase1a.attempt": _text(ledger.get("attempt_id"), "attempt ID"),
        "clinic.phase1a.claim": _text(claim.get("claim_id"), "claim ID"),
        "clinic.phase1a.runner-create-intent": _text(
            creation.get("intent_sha256"),
            "runner intent hash",
        ),
        "clinic.phase1a.service": _text(
            creation.get("service_name"),
            "runner service",
        ),
    }


def select_runner_candidate(
    containers: list[RunnerContainer],
    *,
    expected_id: str | None,
    expected_name: str,
    expected_labels: dict[str, str],
) -> RunnerContainer | None:
    """Select the ID/name/intent-label union and require total agreement."""
    intent = expected_labels["clinic.phase1a.runner-create-intent"]
    matches = [
        item
        for item in containers
        if item.identifier == expected_id
        or item.name == expected_name
        or item.labels.get("clinic.phase1a.runner-create-intent") == intent
    ]
    if not matches:
        return None
    if len(matches) != 1:
        _fail("runner selector union is ambiguous")
    candidate = matches[0]
    if (
        (expected_id is not None and candidate.identifier != expected_id)
        or candidate.name != expected_name
        or candidate.labels != expected_labels
    ):
        _fail("runner selector union disagrees")
    return candidate


def validate_runner_config(container: RunnerContainer, service: JsonObject) -> None:
    """Require byte-equivalent nonsecret create configuration before mutation."""
    expected_user = f"{service.get('uid')}:{service.get('gid')}"
    if container.image_id != service.get("image_id"):
        _fail("runner image identity drifted")
    if container.user != expected_user:
        _fail("runner user identity drifted")
    if container.command != tuple(_strings(service.get("command"), "runner command")):
        _fail("runner command drifted")
    _validate_environment(container.environment, service)
    if container.network_mode != service.get("network_mode"):
        _fail("runner network mode drifted")
    if container.extra_hosts != _expected_extra_hosts(service):
        _fail("runner extra-host mapping drifted")
    if container.mounts != _expected_mounts(service):
        _fail("runner volume mapping drifted")
    if service.get("published_ports") != [] or not container.ports_empty:
        _fail("runner unexpectedly publishes a port")
    _validate_filesystem(container, service.get("filesystem_contract"))
    if not (container.auto_remove and container.open_stdin and container.attach_stdin):
        _fail("runner disposable interactive configuration drifted")


def runner_created_observation(
    container: RunnerContainer,
    claim: JsonObject,
) -> JsonObject:
    """Project one exact inert container into the closed stack observation."""
    desired = _object(claim.get("desired"), "runner desired")
    services = _objects(desired.get("services"), "runner services")
    if len(services) != 1:
        _fail("runner requires exactly one desired service")
    service = services[0]
    observed_service: JsonObject = {
        "command": service["command"],
        "container_id": container.identifier,
        "environment_contract": service["environment_contract"],
        "extra_hosts": service["extra_hosts"],
        "filesystem_contract": service["filesystem_contract"],
        "gid": service["gid"],
        "image_contract": service["image_contract"],
        "image_id": service["image_id"],
        "name": service["name"],
        "network_attachments": [],
        "network_mode": service["network_mode"],
        "published_ports": service["published_ports"],
        "state": "created",
        "uid": service["uid"],
        "volume_mounts": service["volume_mounts"],
    }
    observed: JsonObject = {
        "borrowed_networks": [],
        "borrowed_volumes": [],
        "container_ids": [container.identifier],
        "listeners": [],
        "owned_networks": [],
        "owned_volumes": [],
        "services": [observed_service],
    }
    validate_stack_observation(observed, claim, required_state="created")
    return observed


def _validate_environment(actual: dict[str, str], service: JsonObject) -> None:
    contract = _object(service.get("environment_contract"), "runner environment")
    expected = {
        _text(item.get("name"), "environment name"): _text(
            item.get("value"),
            "environment value",
            allow_empty=True,
        )
        for item in _objects(contract.get("literal"), "literal environment")
    }
    if any(actual.get(name) != value for name, value in expected.items()):
        _fail("runner literal environment drifted")
    absent = _strings(contract.get("absent_keys"), "absent environment")
    if set(absent).intersection(actual):
        _fail("runner forbidden environment key is present")


def _expected_extra_hosts(service: JsonObject) -> tuple[str, ...]:
    return tuple(
        f"{_text(item.get('hostname'), 'extra-host name')}:"
        f"{_text(item.get('address'), 'extra-host address')}"
        for item in _objects(service.get("extra_hosts"), "runner extra hosts")
    )


def _expected_mounts(service: JsonObject) -> tuple[RunnerMount, ...]:
    values = [
        RunnerMount(
            _text(item.get("volume_name"), "volume name"),
            _text(item.get("target"), "volume target"),
            item.get("read_only") is True,
        )
        for item in _objects(service.get("volume_mounts"), "runner volume mounts")
    ]
    return tuple(sorted(values, key=lambda item: (item.name, item.target)))


def _validate_filesystem(container: RunnerContainer, value: JsonValue) -> None:
    if value is None:
        if (
            container.read_only
            or container.tmpfs
            or container.ipc_mode != "private"
            or container.shm_size != DEFAULT_SHM_SIZE
        ):
            _fail("runner default filesystem configuration drifted")
        return
    contract = _object(value, "runner filesystem contract")
    tmpfs = {
        _text(item.get("target"), "tmpfs target"): runner_tmpfs_options(item)
        for item in _objects(contract.get("tmpfs_mounts"), "runner tmpfs mounts")
    }
    if (
        container.read_only is not (contract.get("root_read_only") is True)
        or container.tmpfs != tmpfs
        or container.ipc_mode != contract.get("ipc_mode")
        or container.shm_size != contract.get("shm_size_bytes")
    ):
        _fail("runner filesystem confinement drifted")


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


def _text(value: JsonValue, context: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
