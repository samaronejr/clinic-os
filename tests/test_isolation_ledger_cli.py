from __future__ import annotations

import importlib
from pathlib import Path
from typing import Protocol, cast

from ops.testing.isolation_common import load_json

from isolation_candidate_fixtures import (
    candidate_envelope,
    reserve_and_activate_candidate,
    write_staged_envelope,
)
from isolation_claim_fixtures import (
    CLAIM_ID,
    filesystem_spec,
    runner_stack_spec,
    snapshot,
    write_spec,
)
from isolation_rejection_fixtures import (
    FailureReceiptSpec,
    empty_inventory,
    write_failure_receipt,
)


class LedgerCli(Protocol):
    def run_cli(self, arguments: list[str]) -> int: ...


def _cli() -> LedgerCli:
    return cast("LedgerCli", importlib.import_module("ops.testing.isolation_ledger"))


def test_claim_and_release_cli_use_the_current_worktree_authority(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    # Given: a feature worktree bound to one temporary canonical ledger.
    ledger_path = snapshot(tmp_path)
    ledger, _ = load_json(ledger_path)
    worktree = Path(str(ledger["worktree_realpath"]))
    spec_path = write_spec(tmp_path, filesystem_spec(CLAIM_ID, []))
    monkeypatch.chdir(worktree)  # type: ignore[attr-defined]

    # When: the exact claim and release forms run without a caller ledger path.
    assert _cli().run_cli(["claim", "--spec", str(spec_path)]) == 0
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    claim_root.rmdir()
    assert _cli().run_cli(["release", "--claim", CLAIM_ID]) == 0

    # Then: authority was resolved through the current worktree's .omo binding.
    released, _ = load_json(ledger_path)
    assert released["claims"] == []


def test_candidate_publish_cli_has_no_caller_selected_output_fields(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    # Given: an active candidate and an inert transition probe.
    ledger_path = snapshot(tmp_path)
    reserve_and_activate_candidate(tmp_path, ledger_path)
    ledger, _ = load_json(ledger_path)
    worktree = Path(str(ledger["worktree_realpath"]))
    claim_root = Path(str(ledger["attempt_root"])) / "claims" / CLAIM_ID
    staged = write_staged_envelope(claim_root, candidate_envelope(ledger))
    calls: list[tuple[Path, str, Path]] = []

    def publish(path: Path, claim_id: str, staged_path: Path) -> bytes:
        calls.append((path, claim_id, staged_path))
        return b'{"bound":true}\n'

    module = importlib.import_module("ops.testing.isolation_ledger")
    monkeypatch.chdir(worktree)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "publish_candidate_envelope", publish)  # type: ignore[attr-defined]

    # When: the closed staged-envelope-only form and one override attempt run.
    assert (
        _cli().run_cli(
            ["candidate-publish", "--claim", CLAIM_ID, "--staged-envelope", str(staged)]
        )
        == 0
    )
    assert (
        _cli().run_cli(
            [
                "candidate-publish",
                "--claim",
                CLAIM_ID,
                "--staged-envelope",
                str(staged),
                "--destination",
                "/foreign",
            ]
        )
        == 2
    )

    # Then: only the exact command reached the transition and emitted bound bytes.
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.out == '{"bound":true}\n'
    assert calls == [(ledger_path, CLAIM_ID, staged)]


def test_verify_and_reconcile_cli_accept_only_the_closed_same_boot_forms(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    # Given: a bound worktree and inert same-boot transition probes.
    ledger_path = snapshot(tmp_path)
    ledger, _ = load_json(ledger_path)
    worktree = Path(str(ledger["worktree_realpath"]))
    calls: list[tuple[str, Path, str | None]] = []

    def verify(path: Path, claim_id: str, *, refresh: bool) -> None:
        assert refresh is True
        calls.append(("verify", path, claim_id))

    def reconcile(path: Path) -> None:
        calls.append(("reconcile", path, None))

    module = importlib.import_module("ops.testing.isolation_ledger")
    monkeypatch.chdir(worktree)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "verify_claim", verify)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "reconcile_same_boot", reconcile)  # type: ignore[attr-defined]

    # When: the required forms and reordered/extended aliases are dispatched.
    assert _cli().run_cli(["verify", "--refresh", "--claim", CLAIM_ID]) == 0
    assert _cli().run_cli(["reconcile"]) == 0
    assert _cli().run_cli(["verify", "--claim", CLAIM_ID, "--refresh"]) == 2
    assert _cli().run_cli(["reconcile", "--unknown"]) == 2

    # Then: only the exact same-boot grammar reaches transition authority.
    assert calls == [
        ("verify", ledger_path, CLAIM_ID),
        ("reconcile", ledger_path, None),
    ]


def test_runner_cli_accepts_only_closed_create_and_discard_grammar(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    # Given: a bound worktree, reserved runner, and inert physical adapters.
    ledger_path = snapshot(tmp_path)
    ledger, _ = load_json(ledger_path)
    worktree = Path(str(ledger["worktree_realpath"]))
    spec = write_spec(tmp_path, runner_stack_spec(CLAIM_ID))
    calls: list[tuple[str, Path, str, str | None]] = []

    def create(path: Path, claim_id: str) -> str:
        calls.append(("create", path, claim_id, None))
        return "e" * 64

    def discard(path: Path, claim_id: str, reason: str) -> None:
        calls.append(("discard", path, claim_id, reason))

    module = importlib.import_module("ops.testing.isolation_ledger")
    monkeypatch.chdir(worktree)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "create_runner", create)  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "discard_runner", discard)  # type: ignore[attr-defined]
    assert _cli().run_cli(["claim", "--spec", str(spec)]) == 0

    # When: exact forms and one caller-selected Docker argument are submitted.
    assert _cli().run_cli(["runner-create", "--claim", CLAIM_ID]) == 0
    assert (
        _cli().run_cli(
            [
                "runner-discard",
                "--claim",
                CLAIM_ID,
                "--reason",
                "owner-cleanup",
            ]
        )
        == 0
    )
    assert (
        _cli().run_cli(["runner-create", "--claim", CLAIM_ID, "--network", "foreign"])
        == 2
    )

    # Then: only the fixed claim/reason authority reaches lifecycle adapters.
    assert calls == [
        ("create", ledger_path, CLAIM_ID, None),
        ("discard", ledger_path, CLAIM_ID, "owner-cleanup"),
    ]


def test_rejection_cli_accepts_only_the_closed_non_user_grammar(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    # Given: one bound claim-free attempt with an authenticated failure receipt.
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
    worktree = Path(str(ledger["worktree_realpath"]))
    control_root = ledger_path.parent / "clinic-os-phase1a-final"
    module = importlib.import_module("ops.testing.isolation_ledger")
    monkeypatch.chdir(worktree)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        module,
        "capture_host_inventory",
        empty_inventory,
    )

    # When: the exact context, reject, and close forms run before one reordered alias.
    assert (
        _cli().run_cli(
            [
                "rejection-context",
                "--sha",
                "a" * 40,
                "--control-root",
                str(control_root),
                "--format",
                "nul",
            ]
        )
        == 0
    )
    context_raw = capsys.readouterr().out  # type: ignore[attr-defined]
    assert context_raw.endswith("F1:infrastructure\0")
    assert (
        _cli().run_cli(
            [
                "reject",
                "--sha",
                "a" * 40,
                "--reason",
                "F1:infrastructure",
                "--inputs-sha256",
                "none",
                "--pre-f4-sha256",
                "none",
                "--final-sha256",
                "none",
            ]
        )
        == 0
    )
    spec_sha = capsys.readouterr().out  # type: ignore[attr-defined]
    assert len(spec_sha.strip()) == 64
    assert _cli().run_cli(["close"]) == 0
    assert (
        _cli().run_cli(
            [
                "reject",
                "--reason",
                "F1:infrastructure",
                "--sha",
                "a" * 40,
                "--inputs-sha256",
                "none",
                "--pre-f4-sha256",
                "none",
                "--final-sha256",
                "none",
            ]
        )
        == 2
    )

    # Then: the ledger is closed only through the exact ordered grammar.
    closed, _ = load_json(ledger_path)
    assert closed["state"] == "closed"
