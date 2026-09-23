from __future__ import annotations

from pathlib import Path

import pytest
from ops.testing.isolation_accepted_close import close_accepted_attempt
from ops.testing.isolation_common import (
    IsolationError,
    load_json,
    raw_sha256,
)

from isolation.isolation_rejection_fixtures import empty_inventory
from isolation.isolation_user_fixtures import user_gate_fixture
from isolation.isolation_user_reboot_fixtures import reboot_user_gate

FINAL_RECEIPT_NAME = "isolation-ledger-final-phase1a.json"
CRASH_STAGES = ("prepared", "ledger-closed", "receipt-published", "complete")


def test_accepted_close_publishes_closed_ledger_before_exact_receipt(
    tmp_path: Path,
) -> None:
    # Given: explicit acceptance after same-boot terminal reconciliation.
    fixture = user_gate_fixture(tmp_path)
    receipt_path = fixture.ledger_path.with_name(FINAL_RECEIPT_NAME)

    # When: accepted close completes and replays after stdout loss.
    result = close_accepted_attempt(
        fixture.ledger_path,
        final_receipt=receipt_path,
        inventory_reader=empty_inventory,
    )
    replayed = close_accepted_attempt(
        fixture.ledger_path,
        final_receipt=receipt_path,
        inventory_reader=empty_inventory,
    )

    # Then: the retained closed ledger, complete journal, and receipt agree exactly.
    ledger, ledger_raw = load_json(fixture.ledger_path)
    attempt_root = Path(str(ledger["attempt_root"]))
    journal, _ = load_json(attempt_root / "accepted-close-state.json")
    receipt, _ = load_json(receipt_path)
    assert result == replayed == receipt_path
    assert ledger["state"] == "closed"
    assert ledger["rejection_close"] is None
    assert journal["phase"] == "complete"
    assert journal["closed_ledger_sha256"] == raw_sha256(ledger_raw)
    assert receipt["closed_ledger_sha256"] == raw_sha256(ledger_raw)
    assert receipt_path.stat().st_mode & 0o777 == 0o400
    assert set(receipt) == {
        "approvals_sha256",
        "attempt_id",
        "closed_at_utc",
        "closed_ledger_sha256",
        "final_sha256",
        "inputs_sha256",
        "pre_f4_sha256",
        "reboot_stable_baseline_sha256",
        "schema_version",
        "sha",
        "terminal_revalidation_sha256",
        "tree_sha",
    }


@pytest.mark.parametrize("crash_stage", CRASH_STAGES)
def test_accepted_close_replays_every_durable_phase(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    # Given: one selected durable accepted-close state boundary.
    fixture = user_gate_fixture(tmp_path)
    receipt_path = fixture.ledger_path.with_name(FINAL_RECEIPT_NAME)

    def crash(stage: str, _state: object) -> None:
        if stage == crash_stage:
            message = f"simulated accepted-close crash at {stage}"
            raise RuntimeError(message)

    # When: close crashes once and the receipt-only/open-prefix form is replayed.
    with pytest.raises(RuntimeError, match="simulated accepted-close crash"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
            checkpoint=crash,
        )
    close_accepted_attempt(
        fixture.ledger_path,
        final_receipt=receipt_path,
        inventory_reader=empty_inventory,
    )

    # Then: recovery converges to the sole closed ledger and immutable receipt.
    ledger, _ = load_json(fixture.ledger_path)
    journal, _ = load_json(
        Path(str(ledger["attempt_root"])) / "accepted-close-state.json"
    )
    assert ledger["state"] == "closed"
    assert journal["phase"] == "complete"
    assert receipt_path.exists()


def test_changed_boot_accepted_close_requires_explicit_terminal_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: terminal reconciliation completed on a boot after attempt creation.
    fixture = user_gate_fixture(tmp_path)
    terminal = reboot_user_gate(fixture, tmp_path, monkeypatch)
    receipt_path = fixture.ledger_path.with_name(FINAL_RECEIPT_NAME)

    # When / Then: omission fails closed, while the captured artifact authorizes close.
    with pytest.raises(IsolationError, match="terminal"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
        )
    close_accepted_attempt(
        fixture.ledger_path,
        final_receipt=receipt_path,
        terminal_revalidation=terminal,
        inventory_reader=empty_inventory,
    )
    ledger, _ = load_json(fixture.ledger_path)
    assert ledger["state"] == "closed"


def test_accepted_close_rejects_intervening_post_terminal_ledger_mutation(
    tmp_path: Path,
) -> None:
    # Given: a terminal artifact followed by an unauthorized ledger replacement.
    fixture = user_gate_fixture(tmp_path)
    fixture.ledger_path.write_bytes(fixture.ledger_path.read_bytes() + b" ")

    # When / Then: accepted close refuses the noncanonical or hash-drifted ledger.
    with pytest.raises((IsolationError, OSError), match=r"JSON|ledger"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=fixture.ledger_path.with_name(FINAL_RECEIPT_NAME),
            inventory_reader=empty_inventory,
        )
