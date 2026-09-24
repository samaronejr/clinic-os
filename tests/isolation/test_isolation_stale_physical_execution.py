from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
import rfc8785
from ops.testing.isolation_candidate_stale_actions import (
    build_candidate_stale_actions,
)
from ops.testing.isolation_common import IsolationError, JsonObject, raw_sha256
from ops.testing.isolation_stale_execution import (
    StaleExecutionAdapters,
    execute_stale_action,
)

from isolation.isolation_candidate_stale_fixtures import fixed_candidate_state

PREVIOUS_BOOT = "11111111-1111-4111-8111-111111111111"
CURRENT_BOOT = "22222222-2222-4222-8222-222222222222"


def test_prior_boot_process_action_rejects_only_the_same_pid_start_identity(
    tmp_path: Path,
) -> None:
    # Given: procfs still exposes the exact PID/start identity from the prior boot.
    proc_root = tmp_path / "proc"
    stat_path = proc_root / "123" / "stat"
    stat_path.parent.mkdir(parents=True)
    stat_path.write_bytes(_proc_stat(123, 456))
    identity = _identity(
        "process-absent-claim",
        "observe-prior-boot-process-absent",
        "process",
        observed_members=[{"pid": 123, "start_ticks": 456}],
        observed_listeners=[],
    )

    # When / Then: the same identity blocks, while PID reuse is authenticated absent.
    with pytest.raises(IsolationError, match="process identity remains"):
        _execute(identity, proc_root=proc_root)
    stat_path.write_bytes(_proc_stat(123, 999))
    _execute(identity, proc_root=proc_root)


def test_stale_staging_removes_only_exact_recorded_files(tmp_path: Path) -> None:
    # Given: one active staging file with an inode/hash-bound observation.
    root = tmp_path / "claims" / "claim"
    output = root / "nested" / "output.json"
    output.parent.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    output.write_bytes(b'{"ok":true}\n')
    output.chmod(0o600)
    observed = _observed_file(output, root)
    identity = _identity(
        "staging-remove-claim",
        "remove-staging",
        "filesystem-staging",
        claim_root_path=str(root),
        desired_owned_files=[
            {
                key: observed[key]
                for key in ("relative_path", "mode", "uid", "gid", "sha256")
            }
        ],
        observed_owned_files=[observed],
    )

    # When: cleanup runs and then replays after the physical deletion.
    _execute(identity)
    _execute(identity)

    # Then: the exact tree is absent and no broad parent deletion occurred.
    assert not root.exists()
    assert root.parent.is_dir()


def test_stale_staging_rejects_a_replaced_symlink_without_unlinking_it(
    tmp_path: Path,
) -> None:
    # Given: a recorded regular file was replaced by a symlink before cleanup.
    root = tmp_path / "claims" / "claim"
    root.mkdir(parents=True, mode=0o700)
    staged = root / "output.json"
    staged.write_bytes(b"bound\n")
    staged.chmod(0o600)
    observed = _observed_file(staged, root)
    staged.unlink()
    staged.symlink_to(tmp_path / "foreign")
    identity = _identity(
        "staging-remove-claim",
        "remove-staging",
        "filesystem-staging",
        claim_root_path=str(root),
        desired_owned_files=[
            {
                key: observed[key]
                for key in ("relative_path", "mode", "uid", "gid", "sha256")
            }
        ],
        observed_owned_files=[observed],
    )

    # When / Then: nofollow validation fails before deleting the replacement.
    with pytest.raises(IsolationError, match="non-regular"):
        _execute(identity)
    assert staged.is_symlink()


def test_stale_candidate_chain_publishes_removes_staging_and_publishes_history(
    tmp_path: Path,
) -> None:
    # Given: a bound candidate with claim-owned staging and empty destinations.
    ledger, claim = fixed_candidate_state(runner=False)
    ledger = deepcopy(ledger)
    claim = deepcopy(claim)
    attempt_root = tmp_path / "attempt"
    ledger["attempt_root"] = str(attempt_root)
    claim["root_relative_path"] = "claims/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    desired = claim["desired"]
    assert isinstance(desired, dict)
    outputs = _objects(desired["published_outputs"])
    outputs[0]["root_path"] = str(attempt_root / "candidate-images")
    outputs[1]["root_path"] = str(attempt_root / "publication-history")
    for authorization in outputs:
        authorization["uid"] = os.geteuid()
        authorization["gid"] = os.getegid()
    observed = claim["observed"]
    assert isinstance(observed, dict)
    observations = _objects(observed["published_outputs"])
    observations[0]["root_path"] = outputs[0]["root_path"]
    observations[1]["root_path"] = outputs[1]["root_path"]
    actions, identities = build_candidate_stale_actions(ledger, claim)
    claim_root = Path(str(ledger["attempt_root"])) / str(claim["root_relative_path"])
    claim_root.mkdir(parents=True, mode=0o700)
    binding = claim["candidate_envelope_binding"]
    assert isinstance(binding, dict)
    envelope = binding["envelope"]
    staged_raw = rfc8785.dumps(envelope) + b"\n"
    staged = claim_root / "candidate-envelope.json"
    staged.write_bytes(staged_raw)
    staged.chmod(0o400)

    # When: all three authenticated outer actions execute in order.
    for action, identity in zip(actions, identities, strict=True):
        execute_stale_action(
            action,
            identity,
            previous_boot_id=PREVIOUS_BOOT,
            current_boot_id=CURRENT_BOOT,
        )

    # Then: immutable envelope/history survive while mutable staging is absent.
    envelope_paths = _strings(outputs[0]["relative_paths"])
    history_paths = _strings(outputs[1]["relative_paths"])
    envelope_path = Path(str(outputs[0]["root_path"])) / envelope_paths[0]
    history_path = Path(str(outputs[1]["root_path"])) / history_paths[0]
    assert envelope_path.read_bytes() == staged_raw
    assert history_path.is_file()
    assert not claim_root.exists()


def _execute(identity: JsonObject, *, proc_root: Path | None = None) -> None:
    action = {
        "action_id": identity["action_id"],
        "claim_id": identity["claim_id"],
        "identity_sha256": hashlib.sha256(rfc8785.dumps(identity)).hexdigest(),
        "operation": identity["operation"],
        "resource_kind": identity["resource_kind"],
    }
    execute_stale_action(
        action,
        identity,
        previous_boot_id=PREVIOUS_BOOT,
        current_boot_id=CURRENT_BOOT,
        adapters=StaleExecutionAdapters(proc_root=proc_root),
    )


def _identity(
    action_id: str,
    operation: str,
    resource_kind: str,
    **extra: object,
) -> JsonObject:
    result: JsonObject = {
        "action_id": action_id,
        "attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "claim_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "operation": operation,
        "origin_claim_sha256": "a" * 64,
        "resource_kind": resource_kind,
        "schema_version": 1,
    }
    result.update(cast("JsonObject", extra))
    return result


def _objects(value: object) -> list[JsonObject]:
    assert isinstance(value, list)
    assert all(isinstance(item, dict) for item in value)
    return cast("list[JsonObject]", value)


def _strings(value: object) -> list[str]:
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return cast("list[str]", value)


def _observed_file(path: Path, root: Path) -> JsonObject:
    value = path.stat()
    return {
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "mode": value.st_mode & 0o7777,
        "relative_path": str(path.relative_to(root)),
        "sha256": raw_sha256(path.read_bytes()),
        "uid": value.st_uid,
    }


def _proc_stat(pid: int, start_ticks: int) -> bytes:
    fields = ["S", "1", str(pid), str(pid)] + ["0"] * 15 + [str(start_ticks)]
    return f"{pid} (worker) {' '.join(fields)}\n".encode()
