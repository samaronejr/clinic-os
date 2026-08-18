from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from ops.testing.isolation_accepted_close import close_accepted_attempt
from ops.testing.isolation_common import JsonObject, load_json, raw_sha256
from ops.testing.isolation_terminal_reconcile import reconcile_terminal_final
from ops.testing.process_helpers import ProcessResult

from isolation_claim_fixtures import FOUNDATION_SHA
from isolation_rejection_fixtures import empty_inventory
from isolation_terminal_reconcile_fixtures import approved_active_publisher_gate
from isolation_user_fixtures import user_gate_fixture
from isolation_user_reboot_fixtures import REBOOT_ID

FINAL_RECEIPT_NAME = "isolation-ledger-final-phase1a.json"
PUBLISHER_CRASH_STAGES = (
    "publisher-release-intent",
    "publisher-ledger-updated",
    "publisher-released",
)


def test_terminal_reconcile_publishes_artifact_before_refresh_and_replays(
    tmp_path: Path,
) -> None:
    # Given: a released sealed gate without its current terminal artifact.
    fixture = user_gate_fixture(tmp_path)
    fixture.terminal_path.unlink()
    ledger_before, _ = load_json(fixture.ledger_path)
    stages: list[str] = []

    def observe(stage: str, _record: JsonObject) -> None:
        stages.append(stage)

    # When: terminal reconciliation refreshes once and is replayed after stdout loss.
    result = reconcile_terminal_final(
        fixture.ledger_path,
        terminal_final=fixture.control_root / "final.json",
        sha=FOUNDATION_SHA,
        inventory_reader=empty_inventory,
        checkpoint=observe,
    )
    artifact_raw = result.read_bytes()
    replayed = reconcile_terminal_final(
        fixture.ledger_path,
        terminal_final=fixture.control_root / "final.json",
        sha=FOUNDATION_SHA,
        inventory_reader=empty_inventory,
    )

    # Then: artifact bytes are immutable and only terminal refresh fields changed.
    ledger_after, ledger_raw = load_json(fixture.ledger_path)
    artifact, _ = load_json(result)
    assert result == replayed == fixture.terminal_path
    assert result.stat().st_mode & 0o777 == 0o400
    assert result.read_bytes() == artifact_raw
    assert artifact["post_update_ledger_sha256"] == raw_sha256(ledger_raw)
    assert stages == ["artifact-published", "ledger-updated", "complete"]
    for key in set(ledger_before) - {"boot_observation", "last_verified_at_utc"}:
        assert ledger_after[key] == ledger_before[key]


def test_terminal_reconcile_recovers_artifact_before_ledger_crash(
    tmp_path: Path,
) -> None:
    # Given: a selected crash immediately after immutable artifact publication.
    fixture = user_gate_fixture(tmp_path)
    fixture.terminal_path.unlink()
    ledger_before = fixture.ledger_path.read_bytes()

    def crash(stage: str, _record: JsonObject) -> None:
        if stage == "artifact-published":
            message = "simulated terminal artifact crash"
            raise RuntimeError(message)

    # When: publication wins, ledger update loses, and reconciliation is replayed.
    with pytest.raises(RuntimeError, match="simulated terminal artifact crash"):
        reconcile_terminal_final(
            fixture.ledger_path,
            terminal_final=fixture.control_root / "final.json",
            sha=FOUNDATION_SHA,
            inventory_reader=empty_inventory,
            checkpoint=crash,
        )
    assert fixture.terminal_path.exists()
    assert fixture.ledger_path.read_bytes() == ledger_before
    reconcile_terminal_final(
        fixture.ledger_path,
        terminal_final=fixture.control_root / "final.json",
        sha=FOUNDATION_SHA,
        inventory_reader=empty_inventory,
    )

    # Then: replay applies exactly the artifact-bound post-refresh ledger.
    artifact, _ = load_json(fixture.terminal_path)
    assert artifact["post_update_ledger_sha256"] == raw_sha256(
        fixture.ledger_path.read_bytes()
    )


def test_terminal_reconcile_rebinds_prepared_acceptance_after_reboot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: explicit acceptance durably prepared on the creation boot.
    fixture = user_gate_fixture(tmp_path)
    receipt_path = fixture.ledger_path.with_name(FINAL_RECEIPT_NAME)

    def stop_prepared(stage: str, _record: JsonObject) -> None:
        if stage == "prepared":
            message = "stop at prepared acceptance"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="prepared acceptance"):
        close_accepted_attempt(
            fixture.ledger_path,
            final_receipt=receipt_path,
            inventory_reader=empty_inventory,
            checkpoint=stop_prepared,
        )
    ledger, _ = load_json(fixture.ledger_path)
    state_path = Path(str(ledger["attempt_root"])) / "accepted-close-state.json"
    predecessor_sha = raw_sha256(state_path.read_bytes())
    _patch_boot(tmp_path, monkeypatch)

    # When: terminal reconciliation creates a fresh boot artifact and close resumes.
    terminal = reconcile_terminal_final(
        fixture.ledger_path,
        terminal_final=fixture.control_root / "final.json",
        sha=FOUNDATION_SHA,
        inventory_reader=empty_inventory,
    )
    artifact, _ = load_json(terminal)
    close_accepted_attempt(
        fixture.ledger_path,
        final_receipt=receipt_path,
        terminal_revalidation=terminal,
        inventory_reader=empty_inventory,
    )

    # Then: the new artifact names the prepared predecessor and close completes.
    closed, _ = load_json(fixture.ledger_path)
    assert artifact["current_boot_id"] == REBOOT_ID
    assert artifact["accepted_close_prepared_sha256"] == predecessor_sha
    assert closed["state"] == "closed"
    assert receipt_path.exists()


@pytest.mark.parametrize("crash_stage", PUBLISHER_CRASH_STAGES)
def test_terminal_reconcile_replays_every_approved_publisher_release_prefix(
    tmp_path: Path,
    crash_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a sealed approval with the persistent publisher still active.
    fixture = approved_active_publisher_gate(tmp_path)

    def clean_bound_worktree(arguments: tuple[str, ...]) -> ProcessResult:
        stdout = f"{FOUNDATION_SHA}\n" if "rev-parse" in arguments else ""
        return ProcessResult(0, stdout, "")

    monkeypatch.setattr(
        "ops.testing.isolation_terminal_publisher_journal.run_process",
        clean_bound_worktree,
    )

    def crash(stage: str, _record: JsonObject) -> None:
        if stage == crash_stage:
            message = f"simulated approved publisher crash at {stage}"
            raise RuntimeError(message)

    # When: one release phase crashes and terminal reconciliation is replayed.
    with pytest.raises(RuntimeError, match="simulated approved publisher crash"):
        reconcile_terminal_final(
            fixture.ledger_path,
            terminal_final=fixture.control_root / "final.json",
            sha=FOUNDATION_SHA,
            inventory_reader=empty_inventory,
            checkpoint=crash,
        )
    reconcile_terminal_final(
        fixture.ledger_path,
        terminal_final=fixture.control_root / "final.json",
        sha=FOUNDATION_SHA,
        inventory_reader=empty_inventory,
    )

    # Then: the ledger has zero claims and the publisher is durably released.
    ledger, _ = load_json(fixture.ledger_path)
    journal, _ = load_json(Path(str(ledger["attempt_root"])) / "final-wave-state.json")
    assert ledger["claims"] == []
    assert journal["terminal_publisher_state"] == "released"
    assert journal["terminal_publisher_release_kind"] == "approved-chain"
    assert fixture.terminal_path.exists()


def _patch_boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    boot_path = tmp_path / "terminal-current-boot-id"
    boot_path.write_text(f"{REBOOT_ID}\n")
    for module_name in (
        "ops.testing.isolation_ledger_store",
        "ops.testing.isolation_terminal_reconcile",
        "ops.testing.isolation_terminal_revalidation",
    ):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "BOOT_ID_PATH", boot_path)
    current = empty_inventory()
    assert current == {
        "containers": [],
        "listeners": [],
        "networks": [],
        "volumes": [],
    }
