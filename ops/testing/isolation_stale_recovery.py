"""Coordinate changed-boot recovery under one stable ledger lock."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from ops.testing.cgroup_probe_recovery import recover_stale_boot_probes
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    raw_sha256,
    utc_now,
)
from ops.testing.isolation_failure_receipts import load_failure_receipts
from ops.testing.isolation_host_inventory import capture_host_inventory
from ops.testing.isolation_ledger_store import BOOT_ID_PATH, locked_recovery_ledger
from ops.testing.isolation_stale_actions import (
    StaleActionPlan,
    build_stale_action_plan,
)
from ops.testing.isolation_stale_journal import (
    create_recovery_journal,
    load_recovery_journal,
    recovery_journal_path,
    replace_recovery_journal,
    validate_recovery_replay,
)
from ops.testing.isolation_stale_physical_recovery import (
    complete_stale_physical_actions,
)
from ops.testing.isolation_stale_proof import publish_resume_proof
from ops.testing.isolation_stale_prune import (
    bind_claims_prune_intent,
    build_pruned_ledger,
    mark_claims_pruned,
)
from ops.testing.isolation_stale_records import (
    RecoveryDiscovery,
    RecoveryPreparation,
    build_prepared_recovery,
)
from ops.testing.isolation_stale_recovery_inputs import (
    boot_observation,
    replay_discovery,
    require_reboot_stable,
    validate_post_prune_prefix,
)
from ops.testing.isolation_stale_reject import finish_reject
from ops.testing.isolation_stale_resume import ProofPublisher, finish_resume
from ops.testing.isolation_terminal_publisher_journal import (
    discover_terminal_publisher,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.isolation_ledger_store import LedgerSession
    from ops.testing.isolation_stale_execution import StaleExecutionAdapters

type InventoryReader = Callable[[], JsonObject]
type Checkpoint = Callable[[str, JsonObject], None]


def reconcile_stale_boot(
    ledger_path: Path,
    *,
    inventory_reader: InventoryReader | None = None,
    checkpoint: Checkpoint | None = None,
    proof_publisher: ProofPublisher | None = None,
    execution_adapters: StaleExecutionAdapters | None = None,
) -> JsonObject:
    """Recover physical resources and prune claims without rebinding the boot."""
    reader = capture_host_inventory if inventory_reader is None else inventory_reader
    emit = _ignore_checkpoint if checkpoint is None else checkpoint
    publisher = publish_resume_proof if proof_publisher is None else proof_publisher
    with locked_recovery_ledger(ledger_path) as session:
        current_boot = BOOT_ID_PATH.read_text().strip()
        recover_stale_boot_probes(session.ledger, current_boot)
        inventory = reader()
        require_reboot_stable(session.ledger, inventory)
        journal_path = recovery_journal_path(session.ledger)
        journal = load_recovery_journal(journal_path)
        plan = _authenticate_or_prepare(
            session,
            journal,
            current_boot,
            inventory,
            emit,
        )
        journal = load_recovery_journal(journal_path)
        if journal is None:
            _fail("prepared stale journal disappeared")
        journal = complete_stale_physical_actions(
            journal_path,
            journal,
            plan,
            emit,
            execution_adapters,
        )
        journal = _prune_claims(session, journal_path, journal, emit)
        if journal.get("recovery_goal") == "reject":
            return finish_reject(session, journal_path, journal, emit)
        _receipts, aggregate = load_failure_receipts(session.ledger)
        if aggregate is not None:
            _fail("resume recovery discovered a failure receipt after selection")
        return finish_resume(session, journal_path, journal, emit, publisher)


def _authenticate_or_prepare(
    session: LedgerSession,
    journal: JsonObject | None,
    current_boot: str,
    inventory: JsonObject,
    emit: Checkpoint,
) -> StaleActionPlan | None:
    journal_path = recovery_journal_path(session.ledger)
    current_hash = raw_sha256(session.original_raw)
    if journal is not None and current_hash != journal.get("prior_ledger_sha256"):
        validate_post_prune_prefix(
            session.ledger,
            session.original_raw,
            journal,
            current_boot,
        )
        return None
    if journal is None:
        timestamp = utc_now()
        observation = boot_observation(current_boot, timestamp, inventory)
        receipts, _aggregate = load_failure_receipts(session.ledger)
        publisher = discover_terminal_publisher(session.ledger)
        discovery = RecoveryDiscovery(
            has_failure_receipts=bool(receipts),
            publisher_claim_id=(None if publisher is None else publisher.claim_id),
            publisher_final_wave_journal_path=(
                None if publisher is None else publisher.journal_path
            ),
            publisher_initial_journal_sha256=(
                None if publisher is None else publisher.initial_journal_sha256
            ),
        )
    else:
        observation = _object(journal.get("boot_observation"), "boot observation")
        timestamp = _text(observation.get("observed_at_utc"), "observation timestamp")
        discovery = replay_discovery(journal)
    plan = build_stale_action_plan(
        session.ledger,
        remove_publisher_staging=(
            bool(discovery.has_failure_receipts)
            or discovery.f3_recovery_required
            or bool(discovery.controller_recoveries)
        ),
    )
    prepared = build_prepared_recovery(
        session.ledger,
        session.original_raw,
        plan,
        RecoveryPreparation(current_boot, observation, discovery, timestamp),
    )
    if journal is None:
        create_recovery_journal(journal_path, prepared)
        emit("journal-prepared", prepared)
    else:
        validate_recovery_replay(journal, prepared)
    return plan


def _prune_claims(
    session: LedgerSession,
    path: Path,
    journal: JsonObject,
    emit: Checkpoint,
) -> JsonObject:
    if journal.get("state") in {
        "resume-proof-published",
        "receipts-finalizing",
        "receipts-finalized",
        "publisher-release-intent",
        "boot-updated",
        "publisher-released",
        "complete",
    }:
        return journal
    if journal.get("state") == "resources-absent":
        _, pruned_raw = build_pruned_ledger(session.ledger, journal)
        journal = bind_claims_prune_intent(journal, pruned_raw)
        replace_recovery_journal(path, journal)
        emit("claims-prune-intent", journal)
    if journal.get("state") == "claims-prune-intent":
        current_hash = raw_sha256(session.original_raw)
        post_hash = journal.get("post_cleanup_ledger_sha256")
        if current_hash == journal.get("prior_ledger_sha256"):
            pruned, pruned_raw = build_pruned_ledger(session.ledger, journal)
            if pruned_raw != session.original_raw:
                session.ledger.clear()
                session.ledger.update(pruned)
                session.commit()
            emit("ledger-pruned", journal)
        elif current_hash != post_hash:
            _fail("ledger is neither prior nor write-ahead pruned bytes")
        journal = mark_claims_pruned(journal, session.original_raw)
        replace_recovery_journal(path, journal)
        emit("claims-pruned", journal)
    if journal.get("state") != "claims-pruned":
        _fail("stale recovery did not reach claims-pruned")
    if raw_sha256(session.original_raw) != journal.get("post_cleanup_ledger_sha256"):
        _fail("claims-pruned ledger differs from bound post-cleanup bytes")
    return journal


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _ignore_checkpoint(_stage: str, _journal: JsonObject) -> None:
    return


def _fail(message: str) -> Never:
    raise IsolationError(message)
