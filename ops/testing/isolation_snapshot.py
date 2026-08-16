"""Publish the authenticated first Phase 1A isolation-ledger snapshot."""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.approved_plan import ApprovedPlan

from ops.testing.approved_plan import freeze_plan
from ops.testing.cgroup_capability_probe import ProbeRequest, run_capability_probe
from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    stable_lock,
    utc_now,
    write_no_replace,
)
from ops.testing.isolation_kickoff_probe import run_kickoff_probe
from ops.testing.isolation_namespace import bind_namespace
from ops.testing.isolation_snapshot_bootstrap import (
    require_recovery_plan_prefix,
    revalidate_bootstrap,
    validate_interrupted_prefix,
    validate_snapshot_destinations,
)
from ops.testing.isolation_snapshot_environment import (
    current_boot_id,
    entry_present,
    snapshot_inventory,
    validate_execution_proof,
)
from ops.testing.isolation_snapshot_inputs import publish_attempt_inputs
from ops.testing.isolation_snapshot_records import (
    ROOT_KEYS,
    SnapshotRootInputs,
    baseline_record,
    execution_proof_record,
    root_record,
)
from ops.testing.isolation_snapshot_recovery import (
    SnapshotRecoveryInputs,
    authenticate_interrupted_snapshot,
)
from ops.testing.isolation_snapshot_types import (
    SnapshotRequest,
    _SnapshotMaterial,
    _SnapshotPaths,
    _SnapshotState,
)
from ops.testing.shared_evidence_baseline import capture_manifest

LEDGER_NAME: Final = "isolation-ledger-phase1a.json"
LOCK_NAME: Final = "isolation-ledger-phase1a.lock"
FOUNDATION_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
__all__ = ("LEDGER_NAME", "LOCK_NAME", "SnapshotRequest", "snapshot_ledger")


def _fail(message: str) -> Never:
    raise IsolationError(message)


def snapshot_ledger(
    request: SnapshotRequest,
    *,
    probe_runner: Callable[[ProbeRequest], Path] = run_capability_probe,
) -> Path:
    """Create the first canonical ledger from authenticated immutable inputs."""
    state = _prepare_snapshot(request)
    with stable_lock(state.paths.lock, create=not state.adopting) as held:
        _enter_locked_snapshot(state, held)
        frozen_plan = _freeze_snapshot_plan(state)
        _revalidate(state, held)
        material = _materialize_snapshot(state, frozen_plan, held)
        _revalidate(state, held)
        publish_attempt_inputs(
            material.attempt_root,
            material.shared_path,
            material.shared_raw,
            material.attempt_id,
            recovering=state.recovering,
        )
        _revalidate(state, held)
        run_kickoff_probe(
            material.attempt_root,
            request.execution_host_preflight,
            state.proof,
            probe_runner,
        )
        validate_execution_proof(state)
        _revalidate(state, held)
    return state.paths.ledger


def _prepare_snapshot(request: SnapshotRequest) -> _SnapshotState:
    if FOUNDATION_PATTERN.fullmatch(request.foundation_sha) is None:
        _fail("foundation SHA must be lowercase 40-hex")
    binding = bind_namespace(
        request.authority_workspace,
        request.authority_root,
        request.worktree,
    )
    boot_id = current_boot_id()
    proof = execution_proof_record(
        request.execution_host_preflight,
        binding.authority_workspace,
        binding.authority_root,
        request.foundation_sha,
        boot_id,
    )
    evidence_root = binding.authority_root / "evidence"
    paths = _SnapshotPaths(
        evidence_root=evidence_root,
        lock=evidence_root / LOCK_NAME,
        ledger=evidence_root / LEDGER_NAME,
        tracked_plan=request.worktree
        / "docs"
        / "plans"
        / "clinic-os-phase1a-approved.md",
    )
    recovering = entry_present(paths.ledger)
    adopting = entry_present(paths.lock)
    if recovering and not adopting:
        _fail("canonical isolation ledger exists without its stable lock")
    attempt_id = "" if recovering else str(uuid.uuid4())
    if not recovering:
        validate_snapshot_destinations(evidence_root, attempt_id)
    return _SnapshotState(
        request=request,
        binding=binding,
        proof=proof,
        boot_id=boot_id,
        inventory=snapshot_inventory(request.inventory_fixture),
        paths=paths,
        recovering=recovering,
        adopting=adopting,
        attempt_id=attempt_id,
    )


def _enter_locked_snapshot(
    state: _SnapshotState,
    held: tuple[int, JsonObject],
) -> None:
    _revalidate(state, held)
    ledger_present = entry_present(state.paths.ledger)
    if not state.recovering and ledger_present:
        _fail("canonical isolation ledger appeared during bootstrap")
    if state.recovering and not ledger_present:
        _fail("interrupted canonical isolation ledger disappeared")
    if not state.recovering:
        validate_snapshot_destinations(state.paths.evidence_root, state.attempt_id)
    if state.adopting and not state.recovering:
        validate_interrupted_prefix(state.paths.evidence_root)


def _freeze_snapshot_plan(state: _SnapshotState) -> ApprovedPlan:
    if state.recovering:
        require_recovery_plan_prefix(
            state.binding.authority_root,
            state.paths.tracked_plan,
        )
    return freeze_plan(
        state.request.approved_plan,
        state.binding.authority_root,
        state.paths.tracked_plan,
        state.paths.tracked_plan.with_suffix(".sha256"),
        adopt_existing=state.adopting,
    )


def _materialize_snapshot(
    state: _SnapshotState,
    frozen_plan: ApprovedPlan,
    held: tuple[int, JsonObject],
) -> _SnapshotMaterial:
    if state.recovering:
        return _recover_snapshot_material(state, frozen_plan, held[1])
    return _publish_snapshot_ledger(state, frozen_plan, held)


def _recover_snapshot_material(
    state: _SnapshotState,
    frozen_plan: ApprovedPlan,
    lock_identity: JsonObject,
) -> _SnapshotMaterial:
    recovered = authenticate_interrupted_snapshot(
        SnapshotRecoveryInputs(
            ledger_path=state.paths.ledger,
            lock_path=state.paths.lock,
            binding=state.binding,
            worktree=state.request.worktree,
            foundation_sha=state.request.foundation_sha,
            boot_id=state.boot_id,
            proof=state.proof,
            inventory=state.inventory,
            approved_plan=frozen_plan.as_json(),
            lock_identity=lock_identity,
        )
    )
    return _SnapshotMaterial(
        recovered.attempt_id,
        recovered.attempt_root,
        recovered.shared_path,
        recovered.shared_raw,
    )


def _publish_snapshot_ledger(
    state: _SnapshotState,
    frozen_plan: ApprovedPlan,
    held: tuple[int, JsonObject],
) -> _SnapshotMaterial:
    manifest = capture_manifest(
        state.paths.evidence_root,
        attempt_id=state.attempt_id,
    )
    attempt_root = (
        state.paths.evidence_root / "clinic-os-phase1a-runtime" / state.attempt_id
    )
    shared_path = attempt_root / "shared-evidence-baseline.json"
    baseline, shared_raw = baseline_record(
        state.binding,
        state.inventory,
        manifest,
        shared_path,
    )
    created_at = utc_now()
    ledger = root_record(
        SnapshotRootInputs(
            attempt_id=state.attempt_id,
            foundation_sha=state.request.foundation_sha,
            binding=state.binding,
            worktree=state.request.worktree,
            boot_id=state.boot_id,
            created_at=created_at,
            lock_path=state.paths.lock,
            lock_identity=held[1],
            attempt_root=attempt_root,
            approved_plan=frozen_plan.as_json(),
            proof=state.proof,
            baseline=baseline,
            inventory=state.inventory,
        )
    )
    if ledger.keys() != ROOT_KEYS:
        _fail("internal ledger root keys drifted")
    _revalidate(state, held)
    write_no_replace(state.paths.ledger, canonical_bytes(ledger), mode=MODE_PRIVATE)
    return _SnapshotMaterial(
        state.attempt_id,
        attempt_root,
        shared_path,
        shared_raw,
    )


def _revalidate(state: _SnapshotState, held: tuple[int, JsonObject]) -> None:
    revalidate_bootstrap(
        state.binding,
        state.request.worktree,
        state.paths.lock,
        held[0],
        held[1],
    )
