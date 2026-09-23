from __future__ import annotations

import importlib
from pathlib import Path
from typing import Protocol, cast

from ops.testing.isolation_common import load_json

from isolation.isolation_rejection_fixtures import empty_inventory
from isolation.isolation_user_fixtures import user_gate_fixture
from isolation_claim_fixtures import FOUNDATION_SHA

CONTROL_ARGUMENT = ".omo/evidence/clinic-os-phase1a-final"


class LedgerCli(Protocol):
    def run_cli(self, arguments: list[str]) -> int: ...


def _cli() -> LedgerCli:
    return cast("LedgerCli", importlib.import_module("ops.testing.isolation_ledger"))


def test_user_rejection_cli_accepts_only_the_frozen_argument_order(
    tmp_path: Path,
    monkeypatch: object,
    capsys: object,
) -> None:
    # Given: sealed terminal evidence exposed through the authenticated worktree.
    fixture = user_gate_fixture(tmp_path)
    ledger, _ = load_json(fixture.ledger_path)
    module = importlib.import_module("ops.testing.isolation_ledger")
    monkeypatch.chdir(Path(str(ledger["worktree_realpath"])))  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        module,
        "capture_host_inventory",
        empty_inventory,
    )

    # When: the exact authorize, context, reject, and close forms are dispatched.
    assert (
        _cli().run_cli(
            [
                "authorize-user-fix",
                "--sha",
                FOUNDATION_SHA,
                "--terminal-revalidation",
                str(fixture.terminal_path),
                "--control-root",
                CONTROL_ARGUMENT,
            ]
        )
        == 0
    )
    binding = Path(capsys.readouterr().out.strip())  # type: ignore[attr-defined]
    assert binding.is_absolute()
    assert (
        _cli().run_cli(
            [
                "rejection-context",
                "--user-requested-fix",
                "--user-authorization",
                str(binding),
                "--sha",
                FOUNDATION_SHA,
                "--control-root",
                CONTROL_ARGUMENT,
                "--format",
                "nul",
            ]
        )
        == 0
    )
    context = tuple(capsys.readouterr().out.rstrip("\0").split("\0"))  # type: ignore[attr-defined]
    reject_arguments = [
        "reject",
        "--user-authorization",
        str(binding),
        "--sha",
        FOUNDATION_SHA,
        "--reason",
        context[9],
        "--inputs-sha256",
        context[1],
        "--pre-f4-sha256",
        context[2],
        "--final-sha256",
        context[3],
    ]
    assert _cli().run_cli(reject_arguments) == 0
    assert len(capsys.readouterr().out.strip()) == 64  # type: ignore[attr-defined]
    assert _cli().run_cli(["close", "--user-authorization", str(binding)]) == 0

    # Then: reordered recovery selectors are rejected and the ledger is closed.
    assert (
        _cli().run_cli(
            [
                "close",
                "--terminal-revalidation",
                str(fixture.terminal_path),
                "--user-authorization",
                str(binding),
            ]
        )
        == 2
    )
    closed, _ = load_json(fixture.ledger_path)
    assert closed["state"] == "closed"
