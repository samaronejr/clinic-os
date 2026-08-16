"""Authorize one user-requested repair after sealed terminal approval."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    directory_identity,
    ensure_private_directory,
    load_json,
    raw_sha256,
    regular_identity,
    write_no_replace,
)
from ops.testing.isolation_failure_receipts import load_failure_receipts
from ops.testing.isolation_ledger_store import locked_open_ledger
from ops.testing.isolation_quiescence import require_quiescent_attempt
from ops.testing.isolation_rejection_evidence import phase_hashes
from ops.testing.isolation_terminal_records import TerminalValidationInputs
from ops.testing.isolation_terminal_revalidation import (
    validate_terminal_revalidation,
)
from ops.testing.isolation_user_fix_records import (
    BINDING_KEYS,
    UserFixAuthorizationRequest,
    UserFixValidationInputs,
    ValidatedUserFixAuthorization,
    binding_record,
    context_fields,
    intent_record,
    require_authorizable_state,
    validate_intent,
)

if TYPE_CHECKING:
    from collections.abc import Callable

BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")

__all__ = (
    "UserFixAuthorizationRequest",
    "UserFixValidationInputs",
    "ValidatedUserFixAuthorization",
    "authorize_user_fix",
    "build_user_rejection_context",
    "validate_user_fix_authorization",
)


def authorize_user_fix(
    ledger_path: Path,
    request: UserFixAuthorizationRequest,
    *,
    inventory_reader: Callable[[], JsonObject],
) -> Path:
    """Publish or validate the intent and current-boot authorization binding."""
    with locked_open_ledger(ledger_path) as session:
        require_quiescent_attempt(ledger_path, session.ledger, inventory_reader)
        _require_no_failure_receipts(session.ledger)
        attempt_root = _attempt_root(session.ledger)
        require_authorizable_state(attempt_root, request.sha)
        terminal = validate_terminal_revalidation(
            TerminalValidationInputs(
                ledger_path,
                session.ledger,
                session.original_raw,
                request.sha,
                request.terminal_revalidation,
                request.control_root,
            )
        )
        intent_path = attempt_root / "user-fix-intent.json"
        intent = intent_record(session.ledger, request.sha, terminal)
        _write_or_validate(intent_path, canonical_bytes(intent))
        _ensure_binding_root(attempt_root / "user-fix-bindings")
        current_boot = _text(
            terminal.artifact.get("current_boot_id"),
            "terminal current boot",
        )
        binding_path = attempt_root / "user-fix-bindings" / f"{current_boot}.json"
        binding = binding_record(
            session.ledger,
            request.sha,
            terminal,
            raw_sha256(intent_path.read_bytes()),
        )
        _write_or_validate(binding_path, canonical_bytes(binding))
        validate_user_fix_authorization(
            UserFixValidationInputs(
                ledger_path,
                session.ledger,
                session.original_raw,
                request.sha,
                binding_path,
                request.control_root,
            )
        )
        return binding_path


def validate_user_fix_authorization(
    inputs: UserFixValidationInputs,
) -> ValidatedUserFixAuthorization:
    """Authenticate only the current boot's complete USER authorization chain."""
    ledger = inputs.ledger
    attempt_root = _attempt_root(ledger)
    current_boot = BOOT_ID_PATH.read_text().strip()
    expected_binding = attempt_root / "user-fix-bindings" / f"{current_boot}.json"
    if inputs.user_authorization != expected_binding:
        _fail("USER authorization is not the current boot binding")
    regular_identity(inputs.user_authorization, mode=MODE_IMMUTABLE)
    binding, binding_raw = load_json(inputs.user_authorization)
    if set(binding) != BINDING_KEYS or binding.get("schema_version") != 1:
        _fail("USER binding has an open or unknown root")
    if (
        binding.get("attempt_id") != ledger.get("attempt_id")
        or binding.get("sha") != inputs.sha
        or binding.get("current_boot_id") != current_boot
        or binding.get("user_fix_intent_relative_path") != "user-fix-intent.json"
    ):
        _fail("USER binding differs from current ledger authority")
    intent_path = attempt_root / "user-fix-intent.json"
    regular_identity(intent_path, mode=MODE_IMMUTABLE)
    intent, intent_raw = load_json(intent_path)
    validate_intent(intent, ledger, inputs.sha)
    if raw_sha256(intent_raw) != binding.get("user_fix_intent_sha256"):
        _fail("USER binding intent hash changed")
    relative_terminal = f"terminal-revalidation/{current_boot}.json"
    if binding.get("terminal_revalidation_relative_path") != relative_terminal:
        _fail("USER binding terminal relative path changed")
    terminal_path = attempt_root / relative_terminal
    terminal = validate_terminal_revalidation(
        TerminalValidationInputs(
            inputs.ledger_path,
            ledger,
            inputs.ledger_raw,
            inputs.sha,
            terminal_path,
            inputs.control_root,
        )
    )
    if (
        raw_sha256(terminal.artifact_raw) != binding.get("terminal_revalidation_sha256")
        or binding.get("boot_changed") != terminal.artifact.get("boot_changed")
        or binding.get("approvals_sha256") != intent.get("approvals_sha256")
        or binding.get("approvals_sha256") != terminal.artifact.get("approvals_sha256")
    ):
        _fail("USER binding differs from terminal or intent evidence")
    return ValidatedUserFixAuthorization(
        intent_path,
        intent,
        intent_raw,
        inputs.user_authorization,
        binding,
        binding_raw,
        terminal,
    )


def build_user_rejection_context(
    ledger_path: Path,
    *,
    sha: str,
    control_root: Path,
    user_authorization: Path,
    inventory_reader: Callable[[], JsonObject],
) -> tuple[str, ...]:
    """Project the fixed USER rejection context after full binding validation."""
    with locked_open_ledger(ledger_path) as session:
        require_quiescent_attempt(ledger_path, session.ledger, inventory_reader)
        _require_no_failure_receipts(session.ledger)
        evidence = validate_user_fix_authorization(
            UserFixValidationInputs(
                ledger_path,
                session.ledger,
                session.original_raw,
                sha,
                user_authorization,
                control_root,
            )
        )
        require_authorizable_state(
            _attempt_root(session.ledger),
            sha,
            raw_sha256(evidence.intent_raw),
        )
        phases = phase_hashes(ledger_path, control_root)
        binding = evidence.binding
        terminal_path = (
            str(evidence.terminal.artifact_path)
            if binding.get("boot_changed") is True
            else "none"
        )
        return context_fields(session.ledger, phases, terminal_path)


def _require_no_failure_receipts(ledger: JsonObject) -> None:
    receipts, aggregate = load_failure_receipts(ledger)
    if receipts or aggregate is not None:
        _fail("USER authorization cannot coexist with failure receipts")


def _ensure_binding_root(path: Path) -> None:
    ensure_private_directory(path)
    identity = directory_identity(path)
    if identity.get("mode") != MODE_DIRECTORY:
        _fail("USER binding root is not private")


def _write_or_validate(path: Path, raw: bytes) -> None:
    try:
        write_no_replace(path, raw, mode=MODE_IMMUTABLE)
    except FileExistsError:
        regular_identity(path, mode=MODE_IMMUTABLE)
        if path.read_bytes() != raw:
            _fail(f"existing USER artifact differs: {path.name}")


def _attempt_root(ledger: JsonObject) -> Path:
    return Path(_text(ledger.get("attempt_root"), "attempt root"))


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
