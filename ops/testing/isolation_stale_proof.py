"""Publish and project the current-boot proof for stale resume."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue
from ops.testing.isolation_snapshot_records import execution_proof_record
from ops.testing.isolation_stale_journal import recovery_journal_path

if TYPE_CHECKING:
    from ops.testing.isolation_ledger_store import LedgerSession

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
PUBLISHER: Final = PROJECT_ROOT / "ops" / "testing" / "execution_host_preflight.py"
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")


def publish_resume_proof(
    session: LedgerSession,
    journal: JsonObject,
) -> JsonObject:
    """Run the closed inherited-lock publisher and authenticate its proof."""
    authority_root = session.path.parent.parent
    binding = _object(session.ledger.get("authority_binding"), "authority binding")
    workspace = Path(
        _text(binding.get("authority_workspace_realpath"), "authority workspace")
    )
    foundation_sha = _text(session.ledger.get("foundation_sha"), "foundation SHA")
    boot_id = _text(journal.get("current_boot_id"), "current boot ID")
    journal_path = recovery_journal_path(session.ledger)
    result = subprocess.run(  # noqa: S603 - fixed interpreter and script path.
        (
            sys.executable,
            PUBLISHER,
            "publish",
            "--authority-root",
            authority_root,
            "--stale-boot-resume",
            journal_path,
            "--stable-lock-fd",
            str(session.lock_descriptor),
        ),
        check=False,
        capture_output=True,
        text=True,
        pass_fds=(session.lock_descriptor,),
    )
    digest = result.stdout.strip()
    if result.returncode != 0 or SHA256.fullmatch(digest) is None:
        detail = result.stderr.strip() or "invalid proof publisher result"
        _fail(detail)
    proof_path = authority_root / f"clinic-os-phase1a-execution-host-{boot_id}.json"
    proof = execution_proof_record(
        proof_path,
        workspace,
        authority_root,
        foundation_sha,
        boot_id,
    )
    if proof.get("sha256") != digest:
        _fail("proof publisher digest differs from authenticated bytes")
    return proof


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
