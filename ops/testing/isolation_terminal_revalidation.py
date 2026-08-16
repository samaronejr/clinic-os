"""Validate the immutable terminal artifact consumed by USER authorization."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Never

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    load_json,
    raw_sha256,
    regular_identity,
)
from ops.testing.isolation_terminal_artifact import (
    TerminalArtifactAuthority,
    validate_terminal_artifact,
)
from ops.testing.isolation_terminal_publisher_contract import (
    validate_final_wave_journal,
)
from ops.testing.isolation_terminal_records import (
    TerminalValidationInputs,
    ValidatedTerminalRevalidation,
)

BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")


def validate_terminal_revalidation(
    inputs: TerminalValidationInputs,
) -> ValidatedTerminalRevalidation:
    """Authenticate the exact current-boot terminal chain without mutation."""
    return _validate_terminal_revalidation(inputs, allow_prepared_predecessor=False)


def validate_accepted_terminal_revalidation(
    inputs: TerminalValidationInputs,
) -> ValidatedTerminalRevalidation:
    """Authenticate terminal evidence for initial or rebound accepted close."""
    return _validate_terminal_revalidation(inputs, allow_prepared_predecessor=True)


def _validate_terminal_revalidation(
    inputs: TerminalValidationInputs,
    *,
    allow_prepared_predecessor: bool,
) -> ValidatedTerminalRevalidation:
    ledger = inputs.ledger
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    current_boot = BOOT_ID_PATH.read_text().strip()
    expected_path = attempt_root / "terminal-revalidation" / f"{current_boot}.json"
    expected_control = inputs.ledger_path.parent / "clinic-os-phase1a-final"
    if inputs.path != expected_path or inputs.control_root != expected_control:
        _fail("terminal USER path escaped its canonical attempt or control root")
    regular_identity(inputs.path, mode=MODE_IMMUTABLE)
    artifact, artifact_raw = load_json(inputs.path)
    validate_terminal_artifact(
        TerminalArtifactAuthority(
            artifact,
            inputs,
            current_boot,
            allow_prepared_predecessor,
        )
    )
    final_raw = _validate_final(inputs.control_root, artifact, inputs.sha)
    publisher_raw = _validate_publisher(
        attempt_root,
        inputs.control_root,
        artifact,
        ledger,
    )
    return ValidatedTerminalRevalidation(
        artifact=artifact,
        artifact_path=inputs.path,
        artifact_raw=artifact_raw,
        final_raw=final_raw,
        publisher_journal_raw=publisher_raw,
    )


def _validate_final(control_root: Path, artifact: JsonObject, sha: str) -> bytes:
    if artifact.get("final_relative_path") != "clinic-os-phase1a-final/final.json":
        _fail("terminal FINAL relative path changed")
    path = control_root / "final.json"
    regular_identity(path, mode=MODE_IMMUTABLE)
    final, raw = load_json(path)
    if raw_sha256(raw) != artifact.get("final_sha256"):
        _fail("terminal FINAL hash changed")
    if (
        final.get("sha") != sha
        or final.get("tree_sha") != artifact.get("tree_sha")
        or final.get("approvals_sha256") != artifact.get("approvals_sha256")
    ):
        _fail("terminal FINAL scope differs from its revalidation")
    return raw


def _validate_publisher(
    attempt_root: Path,
    control_root: Path,
    artifact: JsonObject,
    ledger: JsonObject,
) -> bytes:
    path = attempt_root / "final-wave-state.json"
    regular_identity(path, mode=MODE_PRIVATE)
    journal, raw = load_json(path)
    validate_final_wave_journal(journal)
    if (
        raw_sha256(raw) != artifact.get("publisher_final_journal_sha256")
        or journal.get("terminal_publisher_state") != "released"
        or journal.get("phase") != "final-frozen"
        or journal.get("attempt_id") != ledger.get("attempt_id")
        or journal.get("sha") != artifact.get("sha")
        or journal.get("control_root") != str(control_root)
        or journal.get("final_sha256") != artifact.get("final_sha256")
    ):
        _fail("released publisher journal differs from terminal authority")
    return raw


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
