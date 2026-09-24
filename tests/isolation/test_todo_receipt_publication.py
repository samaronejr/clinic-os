from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Final, cast

import pytest
from ops.testing import todo_receipt_cli
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_no_replace,
)
from ops.testing.todo_receipt_publication import publish_validated_receipt

from isolation_claim_fixtures import (
    CLAIM_ID,
    claim_transitions,
    snapshot,
    write_immutable_json,
)

DESTINATION_NAME: Final = "task-1-clinic-os-phase-1a-staff-scheduling.json"
RECEIPT: Final[JsonObject] = {"schema_version": 1}
RAW: Final = canonical_bytes(RECEIPT)


def _active_publication_claim(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    ledger_path = snapshot(tmp_path)
    ledger, _ = load_json(ledger_path)
    attempt_root = Path(cast("str", ledger["attempt_root"]))
    destination = attempt_root / "todo-evidence" / DESTINATION_NAME
    spec: JsonObject = {
        "claim_id": CLAIM_ID,
        "dependency_claim_ids": [],
        "desired": {
            "owned_files": [
                {
                    "gid": os.getegid(),
                    "mode": 0o600,
                    "relative_path": "receipt.json",
                    "sha256": raw_sha256(RAW),
                    "uid": os.geteuid(),
                }
            ],
            "published_outputs": [
                {
                    "authorization_id": "todo-receipt",
                    "gid": os.getegid(),
                    "governing_lock": "stable",
                    "mode": 0o400,
                    "output_kind": "todo-evidence",
                    "predecessor_authorization_ids": [],
                    "relative_paths": [DESTINATION_NAME],
                    "root_path": str(destination.parent),
                    "uid": os.geteuid(),
                }
            ],
        },
        "kind": "filesystem",
        "purpose": "todo-evidence-staging",
    }
    spec_path = write_immutable_json(tmp_path / "todo-claim.json", spec)
    transitions = claim_transitions()
    transitions.reserve_claim(ledger_path, spec_path)
    claim_root = attempt_root / "claims" / CLAIM_ID
    staged = claim_root / "receipt.json"
    write_no_replace(staged, RAW, mode=0o600)
    identity = staged.stat(follow_symlinks=False)
    observed: JsonObject = {
        "owned_files": [
            {
                "device": identity.st_dev,
                "gid": identity.st_gid,
                "inode": identity.st_ino,
                "mode": stat.S_IMODE(identity.st_mode),
                "relative_path": "receipt.json",
                "sha256": raw_sha256(RAW),
                "uid": identity.st_uid,
            }
        ],
        "published_outputs": [
            {
                "authorization_id": "todo-receipt",
                "entries": [],
                "governing_lock": "stable",
                "output_kind": "todo-evidence",
                "root_path": str(destination.parent),
                "status": "unpublished",
            }
        ],
    }
    observed_path = write_immutable_json(tmp_path / "todo-observed.json", observed)
    transitions.activate_claim(ledger_path, CLAIM_ID, observed_path)
    return tmp_path / "feature", ledger_path, staged, destination


def test_todo_receipt_publication_is_validator_gated_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given: an active claim owns one mode-0600 staged canonical receipt.
    worktree, ledger_path, staged, destination = _active_publication_claim(tmp_path)
    monkeypatch.setattr(todo_receipt_cli, "PROJECT_ROOT", worktree)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish-todo-receipt",
            "--staged",
            str(staged),
            "--destination",
            str(destination),
            "--commit",
            "a" * 40,
        ],
    )
    validated: list[tuple[JsonObject, str]] = []

    def validator(receipt: JsonObject, commit: str) -> bytes:
        validated.append((receipt, commit))
        return RAW

    # When: the CLI validates, publishes, and replays the identical staged bytes.
    assert todo_receipt_cli.run_cli(validator) == 0
    first_ledger = ledger_path.read_bytes()
    assert todo_receipt_cli.run_cli(validator) == 0

    # Then: validation ran twice and immutable publication was adopted byte-for-byte.
    assert validated == [(RECEIPT, "a" * 40), (RECEIPT, "a" * 40)]
    assert destination.read_bytes() == RAW
    assert stat.S_IMODE(destination.stat().st_mode) == 0o400
    assert ledger_path.read_bytes() == first_ledger
    assert capsys.readouterr().out.splitlines() == [raw_sha256(RAW)] * 2


def test_todo_receipt_publication_rejects_existing_different_bytes(
    tmp_path: Path,
) -> None:
    # Given: an active publication claim and a foreign existing destination.
    worktree, _, staged, destination = _active_publication_claim(tmp_path)
    write_no_replace(destination, b'{"foreign":true}\n', mode=0o400)

    # When: publication attempts to adopt that destination.
    with pytest.raises(IsolationError, match="differs"):
        publish_validated_receipt(worktree, staged, destination, RAW)

    # Then: the foreign bytes remain unchanged and are never replaced.
    assert destination.read_bytes() == b'{"foreign":true}\n'


def test_todo_receipt_cli_rejects_validator_byte_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the validator returns bytes different from the staged canonical file.
    worktree, _, staged, destination = _active_publication_claim(tmp_path)
    monkeypatch.setattr(todo_receipt_cli, "PROJECT_ROOT", worktree)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "publish-todo-receipt",
            "--staged",
            str(staged),
            "--destination",
            str(destination),
            "--commit",
            "a" * 40,
        ],
    )

    def validator(_receipt: JsonObject, _commit: str) -> bytes:
        return b"{}"

    # When: the CLI compares validator output with staged input identity.
    assert todo_receipt_cli.run_cli(validator) == 2

    # Then: no immutable destination is created.
    assert not destination.exists()
