"""Create the next isolation attempt after a completed rejection archive."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.cgroup_capability_probe import ProbeRequest, run_capability_probe
from ops.testing.isolation_after_archive_records import (
    AfterArchiveSnapshotRequest,
    SuccessorLedgerInputs,
    publish_or_replay_successor_ledger,
    successor_ledger_record,
    validate_successor_request,
)
from ops.testing.isolation_after_archive_validation import (
    ValidatedRetryArchive,
    validate_retry_archive,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    ensure_private_directory,
    raw_sha256,
    regular_identity,
    stable_lock,
    write_no_replace,
)
from ops.testing.isolation_host_inventory import capture_host_inventory
from ops.testing.isolation_inventory import normalize_inventory
from ops.testing.isolation_kickoff_probe import run_kickoff_probe
from ops.testing.isolation_lineage import (
    SuccessorLineageSeedInputs,
    successor_lineage_seed,
)
from ops.testing.isolation_namespace import bind_namespace
from ops.testing.isolation_snapshot import (
    FOUNDATION_PATTERN,
    LEDGER_NAME,
    LOCK_NAME,
)
from ops.testing.isolation_snapshot_records import (
    baseline_record,
    execution_proof_record,
)
from ops.testing.shared_evidence_baseline import capture_manifest

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ("AfterArchiveSnapshotRequest", "snapshot_after_archive")

BASE_ATTEMPT_ENTRIES: Final = {
    "final-failure-receipts",
    "receipt-lineage-seed.json",
    "shared-evidence-baseline.json",
}
COMPLETE_ATTEMPT_ENTRIES: Final = BASE_ATTEMPT_ENTRIES | {"execution-host-probes"}


def snapshot_after_archive(
    request: AfterArchiveSnapshotRequest,
    *,
    probe_runner: Callable[[ProbeRequest], Path] = run_capability_probe,
) -> Path:
    """Publish or replay one exact successor ledger under the retained lock."""
    if FOUNDATION_PATTERN.fullmatch(request.foundation_sha) is None:
        _fail("foundation SHA must be lowercase 40-hex")
    binding = bind_namespace(
        request.authority_workspace,
        request.authority_root,
        request.worktree,
    )
    evidence_root = binding.authority_root / "evidence"
    ledger_path = evidence_root / LEDGER_NAME
    lock_path = evidence_root / LOCK_NAME
    inventory = _inventory(request.inventory_fixture)
    with stable_lock(lock_path, create=False) as (_descriptor, lock_identity):
        archived = validate_retry_archive(
            ledger_path,
            request.closed_attempt,
            lock_identity,
            lambda: inventory,
        )
        approved_plan = validate_successor_request(request, binding, archived)
        boot_id = _current_boot_id()
        proof = execution_proof_record(
            request.execution_host_preflight,
            binding.authority_workspace,
            binding.authority_root,
            request.foundation_sha,
            boot_id,
        )
        attempt_id = archived.successor_attempt_id
        attempt_root = evidence_root / "clinic-os-phase1a-runtime" / attempt_id
        shared_path = attempt_root / "shared-evidence-baseline.json"
        baseline, shared_raw = baseline_record(
            binding,
            inventory,
            capture_manifest(evidence_root, attempt_id=attempt_id),
            shared_path,
        )
        seed = _successor_seed(archived, attempt_id)
        _publish_attempt_inputs(attempt_root, shared_path, shared_raw, seed)
        expected = successor_ledger_record(
            ledger_path,
            SuccessorLedgerInputs(
                request=request,
                binding=binding,
                lock_path=lock_path,
                lock_identity=lock_identity,
                attempt_id=attempt_id,
                attempt_root=attempt_root,
                approved_plan=approved_plan,
                proof=proof,
                baseline=baseline,
                inventory=inventory,
            ),
        )
        publish_or_replay_successor_ledger(ledger_path, expected)
        run_kickoff_probe(
            attempt_root,
            request.execution_host_preflight,
            proof,
            probe_runner,
        )
        if {path.name for path in attempt_root.iterdir()} != COMPLETE_ATTEMPT_ENTRIES:
            _fail("successor attempt root contains an unauthorized entry")
        if (
            execution_proof_record(
                request.execution_host_preflight,
                binding.authority_workspace,
                binding.authority_root,
                request.foundation_sha,
                boot_id,
            )
            != proof
        ):
            _fail("execution-host proof changed during the kickoff probe")
    return ledger_path


def _successor_seed(
    archived: ValidatedRetryArchive,
    attempt_id: str,
) -> JsonObject:
    prior_id = str(archived.ledger["attempt_id"])
    rejected_sha = archived.tombstone.get("sha")
    if not isinstance(rejected_sha, str):
        _fail("archive tombstone lacks its rejected SHA")
    relative_bundle = (
        f".omo/evidence/clinic-os-phase1a-rejected/{prior_id}/{rejected_sha}"
    )
    lineage_sha = archived.tombstone.get("receipt_lineage_sha256")
    if not isinstance(lineage_sha, str):
        _fail("ordinary archive lacks a final receipt lineage hash")
    return successor_lineage_seed(
        SuccessorLineageSeedInputs(
            prior_attempt_root=archived.bundle / "attempt",
            prior_attempt_id=prior_id,
            current_attempt_id=attempt_id,
            bundle_path=relative_bundle,
            tombstone_sha256=raw_sha256(archived.tombstone_raw),
            lineage_sha256=lineage_sha,
        )
    )


def _publish_attempt_inputs(
    attempt_root: Path,
    shared_path: Path,
    shared_raw: bytes,
    seed: JsonObject,
) -> None:
    ensure_private_directory(attempt_root.parent)
    ensure_private_directory(attempt_root)
    ensure_private_directory(attempt_root / "final-failure-receipts")
    _write_or_validate(shared_path, shared_raw)
    _write_or_validate(
        attempt_root / "receipt-lineage-seed.json",
        canonical_bytes(seed),
    )
    names = {path.name for path in attempt_root.iterdir()}
    if names not in (BASE_ATTEMPT_ENTRIES, COMPLETE_ATTEMPT_ENTRIES):
        _fail("successor attempt root contains an unauthorized entry")


def _write_or_validate(path: Path, raw: bytes) -> None:
    try:
        write_no_replace(path, raw, mode=MODE_IMMUTABLE)
    except FileExistsError:
        regular_identity(path, mode=MODE_IMMUTABLE)
        if path.read_bytes() != raw:
            _fail(f"existing successor input differs: {path.name}")


def _inventory(fixture: object | None) -> JsonObject:
    if fixture is None:
        return capture_host_inventory()
    return normalize_inventory(fixture)


def _current_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def _fail(message: str) -> Never:
    raise IsolationError(message)
