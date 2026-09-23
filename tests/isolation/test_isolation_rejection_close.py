from __future__ import annotations

from pathlib import Path

import pytest
from ops.testing.isolation_close import close_rejected_attempt
from ops.testing.isolation_common import IsolationError, JsonObject, load_json
from ops.testing.isolation_failure_receipts import load_failure_receipts
from ops.testing.isolation_rejection import (
    RejectionRequest,
    build_rejection_context,
    reject_attempt,
)

from isolation.isolation_rejection_fixtures import (
    FailureReceiptSpec,
    empty_inventory,
    write_failure_receipt,
)
from isolation_claim_fixtures import FOUNDATION_SHA, snapshot


def _request(*reasons: str) -> RejectionRequest:
    return RejectionRequest(
        sha=FOUNDATION_SHA,
        reasons=reasons,
        inputs_sha256=None,
        pre_f4_sha256=None,
        final_sha256=None,
    )


def test_rejection_context_projects_authenticated_receipts_and_authority(
    tmp_path: Path,
) -> None:
    # Given: a baseline-clean claim-free attempt with one authenticated receipt.
    ledger_path = snapshot(tmp_path)
    write_failure_receipt(
        ledger_path,
        FailureReceiptSpec(
            lane="F1",
            cause_code="boot-changed",
            failure_class="infrastructure",
            stage="F1-review",
        ),
    )
    ledger, _ = load_json(ledger_path)
    control_root = ledger_path.parent / "clinic-os-phase1a-final"

    # When: the read-only rejection context is projected from fixed authority.
    context = build_rejection_context(
        ledger_path,
        sha=FOUNDATION_SHA,
        control_root=control_root,
        inventory_reader=empty_inventory,
    )

    # Then: no caller value can substitute attempt, authority, phase, or reason data.
    binding = ledger["authority_binding"]
    plan = ledger["approved_plan"]
    assert isinstance(binding, dict)
    assert isinstance(plan, dict)
    assert context == (
        ledger["attempt_id"],
        "none",
        "none",
        "none",
        binding["authority_workspace_realpath"],
        binding["authority_root_realpath"],
        plan["path"],
        ledger["worktree_realpath"],
        "none",
        "F1:infrastructure",
    )


def test_reject_publishes_one_idempotent_evidence_only_spec(tmp_path: Path) -> None:
    # Given: one infrastructure receipt and no live or claimed task resource.
    ledger_path = snapshot(tmp_path)
    write_failure_receipt(
        ledger_path,
        FailureReceiptSpec(
            lane="F1",
            cause_code="boot-changed",
            failure_class="infrastructure",
            stage="F1-review",
        ),
    )
    ledger, ledger_before = load_json(ledger_path)
    _receipts, aggregate = load_failure_receipts(ledger)

    # When: rejection is published and replayed after hypothetical stdout loss.
    first = reject_attempt(
        ledger_path,
        _request("F1:infrastructure"),
        inventory_reader=empty_inventory,
    )
    second = reject_attempt(
        ledger_path,
        _request("F1:infrastructure"),
        inventory_reader=empty_inventory,
    )

    # Then: immutable bytes are reused and the canonical ledger stays open unchanged.
    rejection_path = Path(str(ledger["attempt_root"])) / "rejection-spec.json"
    spec, spec_raw = load_json(rejection_path)
    assert first == second
    assert rejection_path.stat().st_mode & 0o777 == 0o400
    assert spec == {
        "attempt_id": ledger["attempt_id"],
        "failure_receipts_sha256": aggregate,
        "final_sha256": None,
        "inputs_sha256": None,
        "pre_f4_sha256": None,
        "rejections": [{"failure_class": "infrastructure", "lane": "F1"}],
        "retry_kind": "evidence-only",
        "schema_version": 1,
        "sha": FOUNDATION_SHA,
        "user_fix_intent_sha256": None,
    }
    assert first == __import__("hashlib").sha256(spec_raw).hexdigest()
    assert ledger_path.read_bytes() == ledger_before


def test_reject_derives_source_fix_and_refuses_reason_substitution(
    tmp_path: Path,
) -> None:
    # Given: authenticated infrastructure and reviewer-finding receipts.
    ledger_path = snapshot(tmp_path)
    write_failure_receipt(
        ledger_path,
        FailureReceiptSpec(
            lane="F1",
            cause_code="boot-changed",
            failure_class="infrastructure",
            stage="F1-review",
        ),
    )
    write_failure_receipt(
        ledger_path,
        FailureReceiptSpec(
            lane="F2",
            cause_code="reviewer-reject",
            failure_class="finding",
            stage="F2-review",
        ),
    )
    ledger, _ = load_json(ledger_path)
    rejection_path = Path(str(ledger["attempt_root"])) / "rejection-spec.json"

    # When: a caller omits the finding, then submits the exact sorted reason set.
    with pytest.raises(IsolationError, match="reasons"):
        reject_attempt(
            ledger_path,
            _request("F1:infrastructure"),
            inventory_reader=empty_inventory,
        )
    reject_attempt(
        ledger_path,
        _request("F1:infrastructure", "F2:finding"),
        inventory_reader=empty_inventory,
    )

    # Then: the mismatch published nothing and the authenticated finding forces
    # source-fix.
    spec, _ = load_json(rejection_path)
    assert spec["retry_kind"] == "source-fix"
    assert spec["rejections"] == [
        {"failure_class": "infrastructure", "lane": "F1"},
        {"failure_class": "finding", "lane": "F2"},
    ]


def test_close_rejected_attempt_is_atomic_idempotent_and_retains_lock(
    tmp_path: Path,
) -> None:
    # Given: an immutable rejection spec and exact empty live baseline.
    ledger_path = snapshot(tmp_path)
    write_failure_receipt(
        ledger_path,
        FailureReceiptSpec(
            lane="F1",
            cause_code="boot-changed",
            failure_class="infrastructure",
            stage="F1-review",
        ),
    )
    spec_sha = reject_attempt(
        ledger_path,
        _request("F1:infrastructure"),
        inventory_reader=empty_inventory,
    )
    open_ledger, _ = load_json(ledger_path)
    lock_path = Path(str(open_ledger["lock_path"]))
    lock_identity = lock_path.stat(follow_symlinks=False)

    # When: close commits and the exact command is replayed after stdout loss.
    close_rejected_attempt(ledger_path, inventory_reader=empty_inventory)
    first_closed = ledger_path.read_bytes()
    close_rejected_attempt(ledger_path, inventory_reader=empty_inventory)

    # Then: one closed root binds the spec while the stable lock inode survives.
    closed, _ = load_json(ledger_path)
    assert ledger_path.read_bytes() == first_closed
    assert closed["state"] == "closed"
    assert closed["closed_at_utc"] == closed["last_verified_at_utc"]
    assert closed["claims"] == []
    assert closed["rejection_close"] == {
        "rejection_spec_sha256": spec_sha,
        "terminal_revalidation_relative_path": None,
        "terminal_revalidation_sha256": None,
        "user_fix_binding_relative_path": None,
        "user_fix_binding_sha256": None,
        "user_fix_intent_relative_path": None,
        "user_fix_intent_sha256": None,
    }
    assert lock_path.stat(follow_symlinks=False).st_ino == lock_identity.st_ino


def test_close_refuses_foreign_inventory_without_mutating_open_ledger(
    tmp_path: Path,
) -> None:
    # Given: a rejected attempt whose live inventory gained a foreign container.
    ledger_path = snapshot(tmp_path)
    write_failure_receipt(
        ledger_path,
        FailureReceiptSpec(
            lane="F1",
            cause_code="boot-changed",
            failure_class="infrastructure",
            stage="F1-review",
        ),
    )
    reject_attempt(
        ledger_path,
        _request("F1:infrastructure"),
        inventory_reader=empty_inventory,
    )
    before = ledger_path.read_bytes()

    def drifted_inventory() -> JsonObject:
        return {
            "containers": [{"id": "f" * 64}],
            "listeners": [],
            "networks": [],
            "volumes": [],
        }

    # When: close revalidates the full same-boot resource baseline.
    with pytest.raises(IsolationError, match="foreign container"):
        close_rejected_attempt(ledger_path, inventory_reader=drifted_inventory)

    # Then: the attempted close cannot stamp or partially rewrite the ledger.
    assert ledger_path.read_bytes() == before
    still_open, _ = load_json(ledger_path)
    assert still_open["state"] == "open"
