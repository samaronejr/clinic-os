"""Build and validate the closed USER authorization records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    load_json,
    raw_sha256,
    regular_identity,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_terminal_records import (
        ValidatedTerminalRevalidation,
    )

INTENT_KEYS: Final = {
    "approvals_sha256",
    "attempt_id",
    "authorized_reason",
    "final_sha256",
    "schema_version",
    "sha",
    "tree_sha",
}
BINDING_KEYS: Final = {
    "approvals_sha256",
    "attempt_id",
    "boot_changed",
    "current_boot_id",
    "schema_version",
    "sha",
    "terminal_revalidation_relative_path",
    "terminal_revalidation_sha256",
    "user_fix_intent_relative_path",
    "user_fix_intent_sha256",
}


@dataclass(frozen=True, slots=True)
class UserFixAuthorizationRequest:
    """Closed selectors for one boot-stable USER repair authorization."""

    sha: str
    terminal_revalidation: Path
    control_root: Path


@dataclass(frozen=True, slots=True)
class ValidatedUserFixAuthorization:
    """Authenticated USER intent, boot binding, and terminal evidence."""

    intent_path: Path
    intent: JsonObject
    intent_raw: bytes
    binding_path: Path
    binding: JsonObject
    binding_raw: bytes
    terminal: ValidatedTerminalRevalidation


@dataclass(frozen=True, slots=True)
class UserFixValidationInputs:
    """Closed ledger and selector inputs for USER authorization validation."""

    ledger_path: Path
    ledger: JsonObject
    ledger_raw: bytes
    sha: str
    user_authorization: Path
    control_root: Path


def intent_record(
    ledger: JsonObject,
    sha: str,
    terminal: ValidatedTerminalRevalidation,
) -> JsonObject:
    """Project terminal scope into one timestamp-free boot-stable intent."""
    artifact = terminal.artifact
    return {
        "approvals_sha256": artifact["approvals_sha256"],
        "attempt_id": ledger["attempt_id"],
        "authorized_reason": "USER:user-requested-fix",
        "final_sha256": artifact["final_sha256"],
        "schema_version": 1,
        "sha": sha,
        "tree_sha": artifact["tree_sha"],
    }


def binding_record(
    ledger: JsonObject,
    sha: str,
    terminal: ValidatedTerminalRevalidation,
    intent_sha256: str,
) -> JsonObject:
    """Project current terminal evidence into one boot-scoped USER binding."""
    artifact = terminal.artifact
    current_boot = artifact["current_boot_id"]
    return {
        "approvals_sha256": artifact["approvals_sha256"],
        "attempt_id": ledger["attempt_id"],
        "boot_changed": artifact["boot_changed"],
        "current_boot_id": current_boot,
        "schema_version": 1,
        "sha": sha,
        "terminal_revalidation_relative_path": (
            f"terminal-revalidation/{current_boot}.json"
        ),
        "terminal_revalidation_sha256": raw_sha256(terminal.artifact_raw),
        "user_fix_intent_relative_path": "user-fix-intent.json",
        "user_fix_intent_sha256": intent_sha256,
    }


def context_fields(
    ledger: JsonObject,
    phases: tuple[str | None, ...],
    terminal_path: str,
) -> tuple[str, ...]:
    """Build the exact nine fixed NUL fields and sole USER reason."""
    authority = _object(ledger.get("authority_binding"), "authority binding")
    plan = _object(ledger.get("approved_plan"), "approved plan")
    values = tuple("none" if value is None else value for value in phases)
    return (
        _text(ledger.get("attempt_id"), "attempt ID"),
        *values,
        _text(authority.get("authority_workspace_realpath"), "authority workspace"),
        _text(authority.get("authority_root_realpath"), "authority root"),
        _text(plan.get("path"), "approved plan path"),
        _text(ledger.get("worktree_realpath"), "worktree"),
        terminal_path,
        "USER:user-requested-fix",
    )


def validate_intent(intent: JsonObject, ledger: JsonObject, sha: str) -> None:
    """Require the exact immutable USER intent scope and closed root."""
    if (
        set(intent) != INTENT_KEYS
        or intent.get("schema_version") != 1
        or intent.get("attempt_id") != ledger.get("attempt_id")
        or intent.get("sha") != sha
        or intent.get("authorized_reason") != "USER:user-requested-fix"
    ):
        _fail("USER intent differs from current authorization scope")


def require_authorizable_state(
    attempt_root: Path,
    sha: str,
    intent_sha256: str | None = None,
) -> None:
    """Reject accepted or non-USER terminal ownership before publication."""
    accepted = attempt_root / "accepted-close-state.json"
    if accepted.exists() or accepted.is_symlink():
        _fail("accepted close already owns this terminal scope")
    path = attempt_root / "rejection-spec.json"
    if not path.exists() and not path.is_symlink():
        return
    regular_identity(path, mode=MODE_IMMUTABLE)
    spec, _raw = load_json(path)
    rejections = [{"failure_class": "user-requested-fix", "lane": "USER"}]
    if (
        spec.get("sha") != sha
        or spec.get("retry_kind") != "source-fix"
        or spec.get("rejections") != rejections
        or spec.get("failure_receipts_sha256") is not None
        or not isinstance(spec.get("user_fix_intent_sha256"), str)
        or (
            intent_sha256 is not None
            and spec.get("user_fix_intent_sha256") != intent_sha256
        )
    ):
        _fail("existing rejection is not the same USER repair request")


def _object(value: object, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
