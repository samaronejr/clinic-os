from __future__ import annotations

import pytest
from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_host_state import require_known_host_state


def test_disappeared_ambient_process_listener_is_reported() -> None:
    # Given: the immutable baseline contains one unrelated process listener.
    listener = _process_listener(port=44679, pid=8984)
    ledger = _ledger([listener])

    # When: that ambient process has permanently exited on the same boot.
    drift = require_known_host_state(ledger, _inventory([]))

    # Then: the absent ambient identity is surfaced instead of wedging the ledger.
    assert drift.disappeared == (listener,)
    assert drift.appeared == ()


def test_new_ambient_process_listener_is_reported() -> None:
    # Given: one unrelated process listener was absent from the baseline.
    listener = _process_listener(port=40129, pid=12001)

    # When: the ambient process begins listening on the same boot.
    drift = require_known_host_state(_ledger([]), _inventory([listener]))

    # Then: the new ambient identity is surfaced instead of treated as authority.
    assert drift.appeared == (listener,)
    assert drift.disappeared == ()


def test_task_owned_listener_missing_fails_closed() -> None:
    listener = _process_listener(port=55495, pid=13001)

    with pytest.raises(IsolationError, match="recorded task listener is missing"):
        require_known_host_state(
            _ledger([], claims=[_process_claim(listener, status="active")]),
            _inventory([]),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("argv_sha256", "b" * 64),
        ("container_id", "unexpected-owner"),
        ("executable_realpath", "/usr/bin/different-tool"),
        ("host", "::1"),
        ("owner_kind", "container"),
        ("pid", 13002),
        ("port", 55496),
        ("process_start_ticks", 130011),
        ("socket_inode", 1300101),
        ("transport", "udp"),
    ],
)
def test_task_owned_listener_field_drift_fails_closed(
    field: str,
    value: JsonValue,
) -> None:
    listener = _process_listener(port=55495, pid=13001)
    drifted = {**listener, field: value}

    with pytest.raises(IsolationError):
        require_known_host_state(
            _ledger([], claims=[_process_claim(listener, status="active")]),
            _inventory([drifted]),
        )


def test_container_bound_baseline_listener_missing_fails_closed() -> None:
    listener = _container_listener(port=5432)

    with pytest.raises(IsolationError, match="baseline listener drifted"):
        require_known_host_state(_ledger([listener]), _inventory([]))


def test_container_bound_baseline_listener_drift_fails_closed() -> None:
    listener = _container_listener(port=5432)
    drifted = {**listener, "socket_inode": 778}

    with pytest.raises(IsolationError, match="baseline listener drifted"):
        require_known_host_state(_ledger([listener]), _inventory([drifted]))


def test_ambient_listener_squatting_reserved_task_port_fails_closed() -> None:
    listener = _process_listener(port=55495, pid=13001)

    with pytest.raises(IsolationError, match="reserved task endpoint"):
        require_known_host_state(
            _ledger([], claims=[_process_claim(listener, status="reserved")]),
            _inventory([listener]),
        )


def test_ambient_listener_squatting_active_task_port_fails_closed() -> None:
    expected = _process_listener(port=55495, pid=13001)
    ambient = _process_listener(port=55495, pid=13002)

    with pytest.raises(IsolationError, match="task listener identity drifted"):
        require_known_host_state(
            _ledger([], claims=[_process_claim(expected, status="active")]),
            _inventory([ambient]),
        )


def test_duplicate_reuseport_rows_on_task_port_fail_closed() -> None:
    expected = _process_listener(port=55495, pid=13001)
    duplicate = _process_listener(port=55495, pid=13002)

    with pytest.raises(IsolationError, match="duplicate listener identity"):
        require_known_host_state(
            _ledger([], claims=[_process_claim(expected, status="active")]),
            _inventory([expected, duplicate]),
        )


def test_unreadable_process_listener_owner_fails_closed() -> None:
    listener = _process_listener(port=40129, pid=12001)
    unreadable = {**listener, "pid": None}

    with pytest.raises(IsolationError, match="listener PID"):
        require_known_host_state(_ledger([]), _inventory([unreadable]))


def test_foreign_container_listener_fails_closed() -> None:
    with pytest.raises(IsolationError, match="foreign container-bound listener"):
        require_known_host_state(_ledger([]), _inventory([_container_listener(5432)]))


@pytest.mark.parametrize("category", ["containers", "networks", "volumes"])
def test_foreign_nonlistener_resource_still_fails_closed(category: str) -> None:
    resource = _resource(category, marker="new")

    with pytest.raises(IsolationError, match=f"foreign {category[:-1]}"):
        require_known_host_state(
            _ledger([]),
            _inventory([], resources={category: [resource]}),
        )


@pytest.mark.parametrize("category", ["containers", "networks", "volumes"])
@pytest.mark.parametrize("change", ["missing", "drifted"])
def test_baseline_nonlistener_resource_still_fails_closed(
    category: str,
    change: str,
) -> None:
    baseline = _resource(category, marker="baseline")
    current = [] if change == "missing" else [_resource(category, marker="changed")]

    with pytest.raises(IsolationError, match=f"baseline {category[:-1]} drifted"):
        require_known_host_state(
            _ledger([], resources={category: [baseline]}),
            _inventory([], resources={category: current}),
        )


def _process_listener(*, port: int, pid: int) -> JsonObject:
    return {
        "argv_sha256": "a" * 64,
        "container_id": None,
        "executable_realpath": "/usr/bin/operator-tool",
        "host": "127.0.0.1",
        "owner_kind": "process",
        "pid": pid,
        "port": port,
        "process_start_ticks": pid * 10,
        "socket_inode": pid * 100,
        "transport": "tcp",
    }


def _container_listener(port: int) -> JsonObject:
    return {
        "argv_sha256": None,
        "container_id": "c" * 64,
        "executable_realpath": None,
        "host": "127.0.0.1",
        "owner_kind": "container",
        "pid": None,
        "port": port,
        "process_start_ticks": None,
        "socket_inode": 777,
        "transport": "tcp",
    }


def _process_claim(listener: JsonObject, *, status: str) -> JsonObject:
    endpoint: JsonObject = {
        "host": listener["host"],
        "port": listener["port"],
        "transport": listener["transport"],
    }
    observed = [] if status == "reserved" else [listener]
    return {
        "desired": {"host_ports": _json_values([endpoint])},
        "kind": "process",
        "observed": {"listeners": _json_values(observed)},
        "status": status,
    }


def _resource(category: str, *, marker: str) -> JsonObject:
    if category == "containers":
        return {"id": "d" * 64, "marker": marker}
    if category == "networks":
        return {"marker": marker, "name": "protected-network"}
    return {"marker": marker, "volume_name": "protected-volume"}


def _ledger(
    listeners: list[JsonObject],
    *,
    claims: list[JsonObject] | None = None,
    resources: dict[str, list[JsonObject]] | None = None,
) -> JsonObject:
    baseline = _inventory(listeners, resources=resources)
    return {
        "baseline": baseline,
        "claims": _json_values([] if claims is None else claims),
    }


def _inventory(
    listeners: list[JsonObject],
    *,
    resources: dict[str, list[JsonObject]] | None = None,
) -> JsonObject:
    result: JsonObject = {
        "containers": [],
        "listeners": _json_values(listeners),
        "networks": [],
        "volumes": [],
    }
    if resources is not None:
        for category, values in resources.items():
            result[category] = _json_values(values)
    return result


def _json_values(values: list[JsonObject]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(values)
    return result
