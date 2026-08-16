from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest
from ops.testing import cgroup_probe_recovery as recovery
from ops.testing.cgroup_capability_probe import ProbeRequest
from ops.testing.cgroup_probe_records import _initial_journal, _validate_completed
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_no_replace,
)

ATTEMPT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROBE_PID = 2_000_000_001
PROCESS_STATES = frozenset(
    {
        "probe-identity",
        "probe-migrated",
        "probe-running",
        "kill-complete",
        "remove-intent",
        "removed",
    }
)


@dataclass(frozen=True, slots=True)
class _RecoveryCase:
    label: str
    state: str
    child_present: bool
    members: tuple[int, ...]
    process_alive: bool
    expected_state: str
    terminal: bool


def _retryable(label: str, state: str, *, child_present: bool = True) -> _RecoveryCase:
    return _RecoveryCase(
        label=label,
        state=state,
        child_present=child_present,
        members=(),
        process_alive=False,
        expected_state="child-ready",
        terminal=False,
    )


def _terminal(
    label: str,
    state: str,
    *,
    child_present: bool = True,
    members: tuple[int, ...] = (),
    process_alive: bool = False,
) -> _RecoveryCase:
    return _RecoveryCase(
        label=label,
        state=state,
        child_present=child_present,
        members=members,
        process_alive=process_alive,
        expected_state="removed",
        terminal=True,
    )


RECOVERY_CASES = (
    _retryable("prepared-before-intent", "prepared", child_present=False),
    _retryable("mkdir-before-child", "mkdir-intent", child_present=False),
    _retryable("mkdir-before-identity", "mkdir-intent"),
    _retryable("child-ready", "child-ready"),
    _retryable("identity-process-gone", "probe-identity"),
    _terminal(
        "migrated-before-kill",
        "probe-migrated",
        members=(PROBE_PID,),
        process_alive=True,
    ),
    _terminal("migrated-after-kill", "probe-migrated"),
    _terminal(
        "running-before-kill",
        "probe-running",
        members=(PROBE_PID,),
        process_alive=True,
    ),
    _terminal("running-after-kill", "probe-running"),
    _terminal("kill-complete", "kill-complete"),
    _terminal("remove-before-rmdir", "remove-intent"),
    _terminal("remove-after-rmdir", "remove-intent", child_present=False),
    _terminal("removed-before-chmod", "removed", child_present=False),
)


@pytest.mark.parametrize("purpose", ["todo1-kickoff", "final-input-freeze"])
@pytest.mark.parametrize("case", RECOVERY_CASES, ids=lambda case: case.label)
def test_same_recovery_engine_closes_every_durable_crash_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
    case: _RecoveryCase,
) -> None:
    # Given: either legal probe purpose stopped at one durable crash prefix.
    request, parent, identity = _probe_request(tmp_path, purpose)
    path, child = _write_prefix(request, parent, identity, case)
    members = list(case.members)
    alive = {"value": case.process_alive}
    kills: list[Path] = []

    def observed_members(_: Path) -> list[int]:
        return list(members)

    def matching_process(_: int, __: int) -> bool:
        return alive["value"]

    def kill_member(control: Path, raw: bytes) -> None:
        assert control == child / "cgroup.kill"
        assert raw == b"1\n"
        kills.append(control)
        members.clear()
        alive["value"] = False

    monkeypatch.setattr(recovery, "_cgroup_members", observed_members)
    monkeypatch.setattr(recovery, "_matching_process", matching_process)
    monkeypatch.setattr(recovery, "_wait_unpopulated", lambda _: None)
    monkeypatch.setattr(recovery, "_write_control", kill_member)

    # When: the owner replays the authenticated same-boot journal.
    result = recovery.recover_same_boot_probe(path, request, parent, identity)

    # Then: retryable prefixes return child-ready and late prefixes close once.
    journal, _ = load_json(path)
    assert journal["state"] == case.expected_state
    assert (result is None) is case.terminal
    assert bool(kills) is bool(case.members and case.process_alive)
    expected_mode = MODE_IMMUTABLE if case.terminal else MODE_PRIVATE
    assert stat.S_IMODE(path.stat().st_mode) == expected_mode
    if case.terminal:
        assert recovery.recover_same_boot_probe(path, request, parent, identity) is None


@pytest.mark.parametrize("purpose", ["todo1-kickoff", "final-input-freeze"])
def test_completed_probe_rejects_an_open_or_unknown_record_root(
    tmp_path: Path,
    purpose: str,
) -> None:
    # Given: an apparent removed journal with an unauthorized extra field.
    request, parent, identity = _probe_request(tmp_path, purpose)
    case = _terminal("removed", "removed", child_present=False)
    path, _ = _write_prefix(request, parent, identity, case)
    journal, _ = load_json(path)
    journal["unexpected"] = True
    path.unlink()
    write_no_replace(path, canonical_bytes(journal), mode=MODE_IMMUTABLE)

    # When / Then: immutable mode cannot make an open schema authoritative.
    with pytest.raises(IsolationError, match=r"open|unknown"):
        _validate_completed(path, request)


def _write_prefix(
    request: ProbeRequest,
    parent: Path,
    identity: JsonObject,
    case: _RecoveryCase,
) -> tuple[Path, Path]:
    child = parent / f"clinic-os-phase1a-probe-{ATTEMPT_ID}-{request.purpose}"
    journal = _initial_journal(request, parent, identity, child)
    needs_identity = case.state not in {"prepared", "mkdir-intent"}
    if needs_identity or case.child_present:
        child.mkdir()
    if needs_identity:
        child_value = child.stat()
        journal["child_device"] = child_value.st_dev
        journal["child_inode"] = child_value.st_ino
    if case.state in PROCESS_STATES:
        journal.update(
            {
                "expected_parent_pid": os.getpid(),
                "probe_barrier_released": case.state
                not in {"probe-identity", "probe-migrated"},
                "probe_pgid": PROBE_PID,
                "probe_pid": PROBE_PID,
                "probe_start_ticks": 9_999,
            }
        )
    journal["state"] = case.state
    if case.state == "removed":
        journal["removal_kind"] = "rmdir"
        journal["removed_at_utc"] = journal["updated_at_utc"]
    if not case.child_present and child.exists():
        child.rmdir()
    path = request.attempt_root / f"execution-host-probes/{request.purpose}.json"
    path.parent.mkdir()
    write_no_replace(path, canonical_bytes(journal), mode=MODE_PRIVATE)
    return path, child


def _probe_request(
    tmp_path: Path,
    purpose: str,
) -> tuple[ProbeRequest, Path, JsonObject]:
    attempt_root = tmp_path / "attempt"
    attempt_root.mkdir()
    parent = tmp_path / "delegated-parent"
    parent.mkdir()
    parent_stat = parent.stat()
    identity: JsonObject = {
        "device": parent_stat.st_dev,
        "gid": os.getegid(),
        "inode": parent_stat.st_ino,
        "uid": os.geteuid(),
    }
    proof: JsonObject = {
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "cgroup_parent_identity": identity,
        "cgroup_parent_path": str(parent),
        "cgroup_relative_path": "/delegated-parent",
    }
    raw = canonical_bytes(proof)
    proof_path = tmp_path / "proof.json"
    write_no_replace(proof_path, raw, mode=MODE_IMMUTABLE)
    return (
        ProbeRequest(ATTEMPT_ID, attempt_root, proof_path, raw_sha256(raw), purpose),
        parent,
        identity,
    )
