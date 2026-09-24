from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import cast

import pytest
from ops.testing.cgroup_probe_child import process_start_ticks
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    load_json,
)
from ops.testing.isolation_process_topology import validate_process_topology

from isolation.isolation_process_fixtures import (
    blocking_python_argv,
    process_spec_for_argv,
    running_process,
)
from isolation_claim_fixtures import (
    CLAIM_ID,
    claim_transitions,
    snapshot,
    write_immutable_json,
    write_spec,
)


def _observation(pid: int, argv: tuple[str, ...]) -> JsonObject:
    command = b"".join(item.encode() + b"\0" for item in argv)
    return {
        "listener_socket_inode": None,
        "listeners": [],
        "members": [
            {
                "argv_sha256": hashlib.sha256(command).hexdigest(),
                "executable_realpath": str(
                    (Path("/proc") / str(pid) / "exe").resolve(strict=True)
                ),
                "gid": os.getegid(),
                "pgid": os.getpgid(pid),
                "pid": pid,
                "ppid": os.getpid(),
                "sid": os.getsid(pid),
                "start_ticks": process_start_ticks(pid),
                "uid": os.geteuid(),
            }
        ],
    }


def _prefork_observation() -> JsonObject:
    executable = "/usr/bin/python3"
    argv_sha = "a" * 64
    members: list[JsonObject] = [
        {
            "argv_sha256": argv_sha,
            "executable_realpath": executable,
            "gid": os.getegid(),
            "pgid": 100,
            "pid": pid,
            "ppid": os.getpid() if pid == 100 else 100,
            "sid": 100,
            "start_ticks": 1000 + pid,
            "uid": os.geteuid(),
        }
        for pid in (100, 101, 102)
    ]
    master = members[0]
    return {
        "listener_socket_inode": 4242,
        "listeners": [
            {
                "argv_sha256": argv_sha,
                "container_id": None,
                "executable_realpath": executable,
                "host": "127.0.0.1",
                "owner_kind": "process",
                "pid": master["pid"],
                "port": 18080,
                "process_start_ticks": master["start_ticks"],
                "socket_inode": 4242,
                "transport": "tcp",
            }
        ],
        "members": cast("JsonValue", members),
    }


def test_single_process_activation_reobserves_proc_identity_and_release(
    tmp_path: Path,
) -> None:
    # Given: a reserved single-process claim and its isolated live child.
    ledger_path = snapshot(tmp_path)
    transitions = claim_transitions()
    argv = blocking_python_argv()
    spec = process_spec_for_argv(
        CLAIM_ID,
        Path(sys.executable),
        argv,
        host_ports=[],
    )
    transitions.reserve_claim(ledger_path, write_spec(tmp_path, spec))
    with running_process(argv) as process:
        observed = _observation(process.pid, argv)
        observed_path = write_immutable_json(tmp_path / "process.json", observed)

        # When: activation compares the fixture with fresh procfs identity.
        transitions.activate_claim(ledger_path, CLAIM_ID, observed_path)

        # Then: active authority records exact identity and blocks live release.
        ledger, _ = load_json(ledger_path)
        claims = ledger["claims"]
        assert isinstance(claims, list)
        assert isinstance(claims[0], dict)
        assert claims[0]["status"] == "active"
        assert claims[0]["observed"] == observed
        claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
        claim_root.rmdir()
        before = ledger_path.read_bytes()
        with pytest.raises(IsolationError, match="live process"):
            transitions.release_claim(ledger_path, CLAIM_ID)
        assert ledger_path.read_bytes() == before

    transitions.release_claim(ledger_path, CLAIM_ID)
    released, _ = load_json(ledger_path)
    assert released["claims"] == []


def test_process_activation_rejects_start_identity_drift_without_mutation(
    tmp_path: Path,
) -> None:
    # Given: a reserved process whose fixture carries a changed start identity.
    ledger_path = snapshot(tmp_path)
    transitions = claim_transitions()
    argv = blocking_python_argv()
    spec = process_spec_for_argv(
        CLAIM_ID,
        Path(sys.executable),
        argv,
        host_ports=[],
    )
    transitions.reserve_claim(ledger_path, write_spec(tmp_path, spec))
    with running_process(argv) as process:
        observed = _observation(process.pid, argv)
        members = observed["members"]
        assert isinstance(members, list)
        assert isinstance(members[0], dict)
        start_ticks = members[0]["start_ticks"]
        assert isinstance(start_ticks, int)
        members[0]["start_ticks"] = start_ticks + 1
        observed_path = write_immutable_json(tmp_path / "process-drift.json", observed)
        before = ledger_path.read_bytes()

        # When: activation reobserves the PID under the stable ledger lock.
        with pytest.raises(IsolationError, match="identity"):
            transitions.activate_claim(ledger_path, CLAIM_ID, observed_path)

        # Then: the drifted process cannot mutate canonical authority.
        assert ledger_path.read_bytes() == before


def test_gunicorn_topology_requires_master_two_workers_and_shared_listener() -> None:
    # Given: one prefork master, two direct workers, and its shared listener.
    observed = _prefork_observation()
    desired: JsonObject = {
        "host_ports": [{"host": "127.0.0.1", "port": 18080, "transport": "tcp"}],
        "process_model": "gunicorn-prefork",
    }

    # When: the closed topology is validated before procfs reinspection.
    validate_process_topology(observed, desired)

    # Then: changing one worker's direct-parent identity fails closed.
    members = observed["members"]
    assert isinstance(members, list)
    assert isinstance(members[1], dict)
    members[1]["ppid"] = 999
    with pytest.raises(IsolationError, match="direct"):
        validate_process_topology(observed, desired)
