"""Read the closed Docker metadata needed for runner lifecycle decisions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Never, cast

from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from ops.testing.isolation_docker_metadata import CommandRunner

HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RunnerMount:
    """Normalize one nonsecret Docker volume attachment."""

    name: str
    target: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class RunnerContainer:
    """Hold only fields needed for exact runner lifecycle decisions."""

    identifier: str
    name: str
    labels: dict[str, str]
    state: str
    pid: int
    restart_count: int
    image_id: str
    user: str
    command: tuple[str, ...]
    environment: dict[str, str]
    network_mode: str
    read_only: bool
    tmpfs: dict[str, str]
    ipc_mode: str
    shm_size: int
    extra_hosts: tuple[str, ...]
    mounts: tuple[RunnerMount, ...]
    ports_empty: bool
    auto_remove: bool
    open_stdin: bool
    attach_stdin: bool


def inspect_runner_containers(run: CommandRunner) -> list[RunnerContainer]:
    """Return all container selectors plus the runner's nonsecret config."""
    identifiers = _identifiers(
        run(("container", "ls", "--all", "--quiet", "--no-trunc"))
    )
    return [_container(run, identifier) for identifier in identifiers]


def _container(run: CommandRunner, identifier: str) -> RunnerContainer:
    observed_id = _inspect(run, identifier, "{{.Id}}")
    if observed_id != identifier:
        _fail("runner container list/inspect identity mismatch")
    return RunnerContainer(
        identifier=identifier,
        name=_inspect(run, identifier, "{{.Name}}").removeprefix("/"),
        labels=_text_mapping(
            _inspect(run, identifier, "{{json .Config.Labels}}"),
            "runner labels",
        ),
        state=_inspect(run, identifier, "{{.State.Status}}"),
        pid=_nonnegative(_inspect(run, identifier, "{{.State.Pid}}"), "runner PID"),
        restart_count=_nonnegative(
            _inspect(run, identifier, "{{.RestartCount}}"),
            "runner restart count",
        ),
        image_id=_inspect(run, identifier, "{{.Image}}"),
        user=_inspect(run, identifier, "{{.Config.User}}"),
        command=_string_tuple(
            _inspect(run, identifier, "{{json .Config.Cmd}}"),
            "runner command",
        ),
        environment=_environment(_inspect(run, identifier, "{{json .Config.Env}}")),
        network_mode=_inspect(run, identifier, "{{.HostConfig.NetworkMode}}"),
        read_only=_boolean(
            _inspect(run, identifier, "{{.HostConfig.ReadonlyRootfs}}"),
            "runner rootfs",
        ),
        tmpfs=_text_mapping(
            _inspect(run, identifier, "{{json .HostConfig.Tmpfs}}"),
            "runner tmpfs",
        ),
        ipc_mode=_inspect(run, identifier, "{{.HostConfig.IpcMode}}"),
        shm_size=_nonnegative(
            _inspect(run, identifier, "{{.HostConfig.ShmSize}}"),
            "runner shared memory",
        ),
        extra_hosts=_string_tuple(
            _inspect(run, identifier, "{{json .HostConfig.ExtraHosts}}"),
            "runner extra hosts",
        ),
        mounts=_mounts(_inspect(run, identifier, "{{json .Mounts}}")),
        ports_empty=_empty_mapping(
            _inspect(run, identifier, "{{json .HostConfig.PortBindings}}"),
            "runner port bindings",
        ),
        auto_remove=_boolean(
            _inspect(run, identifier, "{{.HostConfig.AutoRemove}}"),
            "runner auto-remove",
        ),
        open_stdin=_boolean(
            _inspect(run, identifier, "{{.Config.OpenStdin}}"),
            "runner open-stdin",
        ),
        attach_stdin=_boolean(
            _inspect(run, identifier, "{{.Config.AttachStdin}}"),
            "runner attach-stdin",
        ),
    )


def _inspect(run: CommandRunner, identifier: str, field: str) -> str:
    return run(("container", "inspect", "--format", field, identifier)).strip()


def _identifiers(raw: str) -> list[str]:
    identifiers = sorted(raw.splitlines())
    if any(HEX64.fullmatch(item) is None for item in identifiers):
        _fail("runner container list has an invalid identifier")
    if len(identifiers) != len(set(identifiers)):
        _fail("runner container list has duplicate identifiers")
    return identifiers


def _environment(raw: str) -> dict[str, str]:
    entries = _string_tuple(raw, "runner environment")
    result: dict[str, str] = {}
    for entry in entries:
        name, separator, value = entry.partition("=")
        if separator != "=" or not name or name in result:
            _fail("runner environment entries are invalid or duplicated")
        result[name] = value
    return result


def _mounts(raw: str) -> tuple[RunnerMount, ...]:
    value = _json(raw, "runner mounts")
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail("runner mounts must be an object array")
    result: list[RunnerMount] = []
    for raw_item in cast("list[dict[object, object]]", value):
        item = {str(key): entry for key, entry in raw_item.items()}
        if item.get("Type") != "volume":
            _fail("runner mount type is not volume")
        name = _text(item.get("Name"), "runner mount name")
        target = _text(item.get("Destination"), "runner mount target")
        read_write = item.get("RW")
        if not isinstance(read_write, bool):
            _fail("runner mount RW flag is invalid")
        result.append(RunnerMount(name, target, not read_write))
    return tuple(sorted(result, key=lambda item: (item.name, item.target)))


def _string_tuple(raw: str, context: str) -> tuple[str, ...]:
    value = _json(raw, context)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} must be a string array")
    return tuple(cast("list[str]", value))


def _text_mapping(raw: str, context: str) -> dict[str, str]:
    value = _json(raw, context)
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        _fail(f"{context} must map strings to strings")
    return cast("dict[str, str]", value)


def _empty_mapping(raw: str, context: str) -> bool:
    value = _json(raw, context)
    if value not in (None, {}):
        _fail(f"{context} must be empty")
    return True


def _json(raw: str, context: str) -> object:
    try:
        return cast("object", json.loads(raw))
    except json.JSONDecodeError as error:
        message = f"invalid {context} JSON"
        raise IsolationError(message) from error


def _boolean(raw: str, context: str) -> bool:
    if raw not in {"true", "false"}:
        _fail(f"{context} is not Boolean")
    return raw == "true"


def _nonnegative(raw: str, context: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        message = f"{context} is not an integer"
        raise IsolationError(message) from error
    if value < 0:
        _fail(f"{context} is negative")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{context} must be a nonempty string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
