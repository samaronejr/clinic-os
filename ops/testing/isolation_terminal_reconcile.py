"""Produce or replay the sole terminal-final ledger revalidation artifact."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_accepted_close_records import (
    validate_accepted_close_state,
)
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    ensure_private_directory,
    load_json,
    raw_sha256,
    regular_identity,
    utc_now,
    write_no_replace,
)
from ops.testing.isolation_ledger_store import locked_recovery_ledger
from ops.testing.isolation_quiescence import require_quiescent_attempt
from ops.testing.isolation_terminal_approval_release import (
    release_approved_terminal_publisher,
)
from ops.testing.isolation_terminal_reconcile_records import (
    PreparedTerminalUpdate,
    TerminalUpdateInputs,
    prepare_terminal_update,
    terminal_observation,
)
from ops.testing.isolation_terminal_records import TerminalValidationInputs
from ops.testing.isolation_terminal_revalidation import (
    validate_accepted_terminal_revalidation,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.testing.isolation_ledger_store import LedgerSession


@dataclass(frozen=True, slots=True)
class _ExistingArtifactOptions:
    sha: str
    current_boot: str
    accepted_predecessor: str | None
    emit: Checkpoint


BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")
type Checkpoint = Callable[[str, JsonObject], None]


def reconcile_terminal_final(
    ledger_path: Path,
    *,
    terminal_final: Path,
    sha: str,
    inventory_reader: Callable[[], JsonObject],
    checkpoint: Checkpoint | None = None,
) -> Path:
    """Publish terminal evidence before its exact post-refresh ledger bytes."""
    emit = checkpoint if checkpoint is not None else _no_checkpoint
    current_boot = BOOT_ID_PATH.read_text().strip()
    with locked_recovery_ledger(ledger_path) as session:
        attempt_root = Path(_text(session.ledger.get("attempt_root"), "attempt root"))
        state_sha = _accepted_predecessor(attempt_root, session.ledger)
        terminal_root = attempt_root / "terminal-revalidation"
        ensure_private_directory(terminal_root)
        artifact_path = terminal_root / f"{current_boot}.json"
        if _present(artifact_path):
            _recover_existing_artifact(
                session,
                artifact_path,
                terminal_final,
                _ExistingArtifactOptions(sha, current_boot, state_sha, emit),
            )
            return artifact_path
        release_approved_terminal_publisher(
            session,
            current_boot,
            terminal_final,
            emit,
        )
        inventory = inventory_reader()
        require_quiescent_attempt(
            session.path,
            session.ledger,
            lambda: inventory,
        )
        observation = terminal_observation(current_boot, inventory, utc_now())
        prepared = prepare_terminal_update(
            TerminalUpdateInputs(
                session.path,
                session.ledger,
                session.original_raw,
                terminal_final,
                current_boot,
                observation,
                state_sha,
            )
        )
        if prepared.artifact.get("sha") != sha:
            _fail("terminal reconcile SHA differs from sealed FINAL")
        write_no_replace(
            artifact_path,
            prepared.artifact_raw,
            mode=MODE_IMMUTABLE,
        )
        emit("artifact-published", prepared.artifact)
        _commit_terminal_ledger(session, prepared)
        emit("ledger-updated", prepared.artifact)
        _validate_complete(
            session,
            artifact_path,
            terminal_final.parent,
            sha,
        )
        emit("complete", prepared.artifact)
        return artifact_path


def _recover_existing_artifact(
    session: LedgerSession,
    artifact_path: Path,
    final_path: Path,
    options: _ExistingArtifactOptions,
) -> None:
    regular_identity(artifact_path, mode=MODE_IMMUTABLE)
    artifact, artifact_raw = load_json(artifact_path)
    current_sha = raw_sha256(session.original_raw)
    if current_sha == artifact.get("pre_update_ledger_sha256"):
        observation = _object(
            artifact.get("boot_observation"),
            "terminal boot observation",
        )
        prepared = prepare_terminal_update(
            TerminalUpdateInputs(
                session.path,
                session.ledger,
                session.original_raw,
                final_path,
                options.current_boot,
                observation,
                options.accepted_predecessor,
            )
        )
        if prepared.artifact_raw != artifact_raw or artifact.get("sha") != options.sha:
            _fail("existing terminal artifact differs from pre-update authority")
        _commit_terminal_ledger(session, prepared)
        options.emit("ledger-updated", artifact)
    elif current_sha != artifact.get("post_update_ledger_sha256"):
        _fail("ledger is neither terminal artifact pre nor post image")
    _validate_complete(
        session,
        artifact_path,
        final_path.parent,
        options.sha,
    )
    options.emit("complete", artifact)


def _commit_terminal_ledger(
    session: LedgerSession,
    prepared: PreparedTerminalUpdate,
) -> None:
    session.ledger.clear()
    session.ledger.update(prepared.ledger)
    session.commit()


def _validate_complete(
    session: LedgerSession,
    artifact_path: Path,
    control_root: Path,
    sha: str,
) -> None:
    validate_accepted_terminal_revalidation(
        TerminalValidationInputs(
            session.path,
            session.ledger,
            session.original_raw,
            sha,
            artifact_path,
            control_root,
        )
    )


def _accepted_predecessor(attempt_root: Path, ledger: JsonObject) -> str | None:
    path = attempt_root / "accepted-close-state.json"
    if not _present(path):
        return None
    regular_identity(path, mode=MODE_PRIVATE)
    state, raw = load_json(path)
    validate_accepted_close_state(state)
    if state.get("phase") != "prepared" or state.get("attempt_id") != ledger.get(
        "attempt_id"
    ):
        _fail("terminal reconciliation requires an open prepared acceptance")
    return raw_sha256(raw)


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _object(value: object, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _no_checkpoint(_stage: str, _record: JsonObject) -> None:
    return


def _fail(message: str) -> Never:
    raise IsolationError(message)
