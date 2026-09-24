from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ops.testing import isolation_ledger
from ops.testing.isolation_common import load_json

from isolation.isolation_rejection_fixtures import empty_inventory
from isolation.isolation_user_fixtures import user_gate_fixture
from isolation.isolation_user_reboot_fixtures import reboot_user_gate
from isolation_claim_fixtures import FOUNDATION_SHA

if TYPE_CHECKING:
    import pytest

FINAL_RECEIPT_ARGUMENT = ".omo/evidence/isolation-ledger-final-phase1a.json"


def test_terminal_reconcile_and_creation_boot_close_use_exact_cli_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: a released final gate with no terminal artifact on its creation boot.
    fixture = user_gate_fixture(tmp_path)
    fixture.terminal_path.unlink()
    ledger, _raw = load_json(fixture.ledger_path)
    monkeypatch.chdir(Path(str(ledger["worktree_realpath"])))
    monkeypatch.setattr(isolation_ledger, "capture_host_inventory", empty_inventory)

    # When: the frozen terminal reconcile and accepted-close commands run exactly.
    status = isolation_ledger.run_cli(
        [
            "reconcile",
            "--terminal-final",
            str(fixture.control_root / "final.json"),
            "--sha",
            FOUNDATION_SHA,
        ]
    )
    terminal_output = capsys.readouterr()
    close_status = isolation_ledger.run_cli(
        ["close", "--final-receipt", FINAL_RECEIPT_ARGUMENT]
    )

    # Then: reconcile prints only the absolute artifact and close seals its receipt.
    receipt = fixture.ledger_path.with_name("isolation-ledger-final-phase1a.json")
    closed, _raw = load_json(fixture.ledger_path)
    assert status == close_status == 0
    assert terminal_output.out == f"{fixture.terminal_path}\n"
    assert terminal_output.err == ""
    assert closed["state"] == "closed"
    assert receipt.stat().st_mode & 0o777 == 0o400


def test_changed_boot_accepted_close_requires_terminal_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: a current terminal artifact produced after the attempt boot changed.
    fixture = user_gate_fixture(tmp_path)
    terminal = reboot_user_gate(fixture, tmp_path, monkeypatch)
    ledger, _raw = load_json(fixture.ledger_path)
    monkeypatch.chdir(Path(str(ledger["worktree_realpath"])))
    monkeypatch.setattr(isolation_ledger, "capture_host_inventory", empty_inventory)

    # When: receipt-only close is refused, then the fixed changed-boot form runs.
    refused = isolation_ledger.run_cli(
        ["close", "--final-receipt", FINAL_RECEIPT_ARGUMENT]
    )
    refusal = capsys.readouterr()
    accepted = isolation_ledger.run_cli(
        [
            "close",
            "--terminal-revalidation",
            str(terminal),
            "--final-receipt",
            FINAL_RECEIPT_ARGUMENT,
        ]
    )

    # Then: only the terminal-bound invocation closes the accepted ledger.
    closed, _raw = load_json(fixture.ledger_path)
    assert refused == 2
    assert "terminal" in refusal.err
    assert accepted == 0
    assert closed["state"] == "closed"


def test_terminal_cli_rejects_reordered_or_relative_final_selectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an otherwise valid released final gate.
    fixture = user_gate_fixture(tmp_path)
    ledger, _raw = load_json(fixture.ledger_path)
    monkeypatch.chdir(Path(str(ledger["worktree_realpath"])))
    monkeypatch.setattr(isolation_ledger, "capture_host_inventory", empty_inventory)

    # When: terminal selectors are reordered or FINAL is not absolute.
    reordered = isolation_ledger.run_cli(
        [
            "reconcile",
            "--sha",
            FOUNDATION_SHA,
            "--terminal-final",
            str(fixture.control_root / "final.json"),
        ]
    )
    relative = isolation_ledger.run_cli(
        [
            "reconcile",
            "--terminal-final",
            ".omo/evidence/clinic-os-phase1a-final/final.json",
            "--sha",
            FOUNDATION_SHA,
        ]
    )

    # Then: neither malformed form mutates the open ledger.
    observed, _raw = load_json(fixture.ledger_path)
    assert reordered == relative == 2
    assert observed["state"] == "open"
