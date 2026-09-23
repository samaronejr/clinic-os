from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from ops.testing.isolation_common import IsolationError, load_json
from ops.testing.isolation_terminal_publisher_journal import (
    discover_terminal_publisher,
)
from ops.testing.process_helpers import ProcessResult, run_process

from isolation.isolation_terminal_publisher_fixtures import stale_publisher_ledger
from isolation_claim_fixtures import FOUNDATION_SHA

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("head", "status"),
    [("b" * 40, ""), (FOUNDATION_SHA, " M tracked.py\n")],
)
def test_terminal_publisher_rejects_wrong_or_dirty_bound_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    head: str,
    status: str,
) -> None:
    ledger_path, _journal_path = stale_publisher_ledger(tmp_path, "active")
    ledger, _ = load_json(ledger_path)

    def inspect(arguments: tuple[str, ...]) -> ProcessResult:
        stdout = f"{head}\n" if "rev-parse" in arguments else status
        return ProcessResult(0, stdout, "")

    monkeypatch.setattr(
        "ops.testing.isolation_terminal_publisher_journal.run_process",
        inspect,
    )
    with pytest.raises(IsolationError, match="clean bound-worktree HEAD"):
        discover_terminal_publisher(ledger)


def test_terminal_publisher_accepts_clean_matching_bound_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path, _journal_path = stale_publisher_ledger(tmp_path, "active")
    ledger, _ = load_json(ledger_path)
    worktree_head = run_process(
        (
            "/usr/bin/git",
            "-C",
            str(ledger["worktree_realpath"]),
            "rev-parse",
            "HEAD",
        )
    ).stdout.strip()

    def inspect(arguments: tuple[str, ...]) -> ProcessResult:
        stdout = f"{worktree_head}\n" if "rev-parse" in arguments else ""
        return ProcessResult(0, stdout, "")

    monkeypatch.setattr(
        "ops.testing.isolation_terminal_publisher_journal.run_process",
        inspect,
    )
    binding = discover_terminal_publisher(ledger)
    assert binding is not None
