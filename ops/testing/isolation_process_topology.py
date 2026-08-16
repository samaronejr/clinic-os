"""Validate closed process member and shared-listener topology."""

from __future__ import annotations

from typing import Final, Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

OBSERVED_KEYS: Final = frozenset({"members", "listener_socket_inode", "listeners"})
MEMBER_KEYS: Final = frozenset(
    {
        "pid",
        "ppid",
        "pgid",
        "sid",
        "start_ticks",
        "uid",
        "gid",
        "executable_realpath",
        "argv_sha256",
    }
)
LISTENER_KEYS: Final = frozenset(
    {
        "transport",
        "host",
        "port",
        "socket_inode",
        "owner_kind",
        "container_id",
        "pid",
        "process_start_ticks",
        "executable_realpath",
        "argv_sha256",
    }
)
PREFORK_MEMBER_COUNT: Final = 3


def _fail(message: str) -> Never:
    raise IsolationError(message)


def validate_process_topology(observed: JsonObject, desired: JsonObject) -> None:
    """Validate the closed single or prefork member and listener topology."""
    if set(observed) != OBSERVED_KEYS:
        _fail("process observation has the wrong closed key set")
    members = _objects(observed["members"], "process members")
    if any(set(member) != MEMBER_KEYS for member in members):
        _fail("process member has the wrong closed key set")
    pids = [_integer(member["pid"], "member PID") for member in members]
    if pids != sorted(set(pids)):
        _fail("process member PIDs are not sorted and unique")
    model = desired.get("process_model")
    if model == "single":
        if len(members) != 1 or not _is_group_leader(members[0]):
            _fail("single process topology is invalid")
    elif model == "gunicorn-prefork":
        _validate_prefork_members(members)
    else:
        _fail("process model is invalid during activation")
    _validate_listener_mapping(observed, desired, members)


def _validate_prefork_members(members: list[JsonObject]) -> None:
    leaders = [member for member in members if _is_group_leader(member)]
    if len(members) != PREFORK_MEMBER_COUNT or len(leaders) != 1:
        _fail("Gunicorn prefork member count or leader is invalid")
    master = leaders[0]
    master_pid = master["pid"]
    workers = [member for member in members if member is not master]
    if any(
        member.get("ppid") != master_pid
        or member.get("pgid") != master_pid
        or member.get("sid") != master_pid
        for member in workers
    ):
        _fail("Gunicorn workers are not direct same-session children")


def _validate_listener_mapping(
    observed: JsonObject,
    desired: JsonObject,
    members: list[JsonObject],
) -> None:
    listeners = _objects(observed["listeners"], "process listeners")
    if any(set(listener) != LISTENER_KEYS for listener in listeners):
        _fail("process listener has the wrong closed key set")
    ports = _objects(desired.get("host_ports"), "desired host ports")
    socket_inode = observed["listener_socket_inode"]
    if not ports:
        if listeners or socket_inode is not None:
            _fail("listener-free process has observed socket identity")
        return
    inode = _integer(socket_inode, "listener socket inode")
    master = next((member for member in members if _is_group_leader(member)), None)
    if master is None or len(listeners) != len(ports):
        _fail("process listener mapping is incomplete")
    expected = sorted((port["transport"], port["host"], port["port"]) for port in ports)
    actual = []
    for listener in listeners:
        if not _listener_owner_matches(listener, master, inode):
            _fail("process listener owner identity drifted")
        actual.append((listener["transport"], listener["host"], listener["port"]))
    if actual != expected:
        _fail("process listener endpoints disagree with desired ports")


def _listener_owner_matches(
    listener: JsonObject,
    master: JsonObject,
    inode: int,
) -> bool:
    return (
        listener.get("socket_inode") == inode
        and listener.get("owner_kind") == "process"
        and listener.get("container_id") is None
        and listener.get("pid") == master.get("pid")
        and listener.get("process_start_ticks") == master.get("start_ticks")
        and listener.get("executable_realpath") == master.get("executable_realpath")
        and listener.get("argv_sha256") == master.get("argv_sha256")
    )


def _is_group_leader(member: JsonObject) -> bool:
    return member.get("pid") == member.get("pgid") == member.get("sid")


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(f"{context} is invalid")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)
