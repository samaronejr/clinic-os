from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from ops.testing import freeze_final_wave_inputs, isolation_final_wave_runtime
from ops.testing.isolation_common import IsolationError
from ops.testing.isolation_ledger_store import locked_open_ledger

from isolation_claim_fixtures import snapshot

if TYPE_CHECKING:
    from pathlib import Path


def test_final_wave_resolves_worktree_evidence_before_ledger_invocation() -> None:
    assert (
        freeze_final_wave_inputs.LEDGER_PATH.resolve()
        == freeze_final_wave_inputs.LEDGER_PATH
    )
    assert (
        isolation_final_wave_runtime.LEDGER.resolve()
        == isolation_final_wave_runtime.LEDGER
    )


def test_ledger_boundary_still_rejects_a_symlink_alias(tmp_path: Path) -> None:
    ledger_path = snapshot(tmp_path)
    worktree = tmp_path / "feature"
    assert (worktree / ".omo").is_symlink()
    alias = worktree / ".omo/evidence/isolation-ledger-phase1a.json"
    assert alias.resolve() == ledger_path

    with locked_open_ledger(alias.resolve()) as session:
        assert session.ledger["state"] == "open"

    with (
        pytest.raises(IsolationError, match="ledger path is noncanonical"),
        locked_open_ledger(alias),
    ):
        pass
