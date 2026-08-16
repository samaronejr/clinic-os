"""Close a rejected isolation attempt without removing its stable lock."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
    utc_now,
)
from ops.testing.isolation_failure_receipts import load_failure_receipts
from ops.testing.isolation_ledger_store import locked_same_boot_ledger
from ops.testing.isolation_quiescence import require_quiescent_attempt
from ops.testing.isolation_user_fix import (
    UserFixValidationInputs,
    validate_user_fix_authorization,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.testing.isolation_common import JsonObject

SHA40_LENGTH: Final = 40


def close_rejected_attempt(
    ledger_path: Path,
    *,
    inventory_reader: Callable[[], JsonObject],
    user_authorization: Path | None = None,
    terminal_revalidation: Path | None = None,
) -> None:
    """Atomically close or replay one receipt-backed rejected attempt."""
    with locked_same_boot_ledger(ledger_path) as session:
        require_quiescent_attempt(ledger_path, session.ledger, inventory_reader)
        if session.ledger.get("state") == "closed":
            rejection_close = _closed_rejection_context(
                ledger_path,
                session.ledger,
                user_authorization,
                terminal_revalidation,
            )
            if session.ledger.get("rejection_close") != rejection_close:
                _fail("closed rejection context differs from immutable evidence")
            return
        if user_authorization is None:
            if terminal_revalidation is not None:
                _fail("non-USER rejection cannot consume terminal revalidation")
            rejection_close = _ordinary_rejection_close(session.ledger)
        else:
            rejection_close = _user_rejection_close(
                ledger_path,
                session.ledger,
                session.original_raw,
                user_authorization,
                terminal_revalidation,
            )
        timestamp = utc_now()
        session.ledger["state"] = "closed"
        session.ledger["closed_at_utc"] = timestamp
        if user_authorization is None:
            session.ledger["last_verified_at_utc"] = timestamp
        session.ledger["rejection_close"] = rejection_close
        session.commit()


def _ordinary_rejection_close(ledger: JsonObject) -> JsonObject:
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    path = attempt_root / "rejection-spec.json"
    regular_identity(path, mode=MODE_IMMUTABLE)
    spec, raw = load_json(path)
    if spec.get("attempt_id") != ledger.get("attempt_id"):
        _fail("rejection specification belongs to another attempt")
    if not _sha40(spec.get("sha")):
        _fail("rejection specification SHA is invalid")
    if spec.get("user_fix_intent_sha256") is not None:
        _fail("USER rejection close requires its authorization binding")
    _receipts, aggregate = load_failure_receipts(ledger)
    if aggregate is None or spec.get("failure_receipts_sha256") != aggregate:
        _fail("rejection receipt aggregate changed before close")
    return {
        "rejection_spec_sha256": raw_sha256(raw),
        "terminal_revalidation_relative_path": None,
        "terminal_revalidation_sha256": None,
        "user_fix_binding_relative_path": None,
        "user_fix_binding_sha256": None,
        "user_fix_intent_relative_path": None,
        "user_fix_intent_sha256": None,
    }


def _user_rejection_close(
    ledger_path: Path,
    ledger: JsonObject,
    ledger_raw: bytes,
    user_authorization: Path,
    terminal_revalidation: Path | None,
) -> JsonObject:
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    spec_path = attempt_root / "rejection-spec.json"
    regular_identity(spec_path, mode=MODE_IMMUTABLE)
    spec, spec_raw = load_json(spec_path)
    sha = _text(spec.get("sha"), "rejection SHA")
    control_root = ledger_path.parent / "clinic-os-phase1a-final"
    evidence = validate_user_fix_authorization(
        UserFixValidationInputs(
            ledger_path,
            ledger,
            ledger_raw,
            sha,
            user_authorization,
            control_root,
        )
    )
    expected_rejections = [{"failure_class": "user-requested-fix", "lane": "USER"}]
    if (
        spec.get("attempt_id") != ledger.get("attempt_id")
        or spec.get("rejections") != expected_rejections
        or spec.get("retry_kind") != "source-fix"
        or spec.get("failure_receipts_sha256") is not None
        or spec.get("user_fix_intent_sha256") != raw_sha256(evidence.intent_raw)
    ):
        _fail("USER rejection specification differs from authorization")
    changed = evidence.binding.get("boot_changed") is True
    expected_terminal = evidence.terminal.artifact_path if changed else None
    if terminal_revalidation != expected_terminal:
        _fail("USER close terminal argument differs from boot binding")
    return {
        "rejection_spec_sha256": raw_sha256(spec_raw),
        "terminal_revalidation_relative_path": str(
            evidence.terminal.artifact_path.relative_to(attempt_root)
        ),
        "terminal_revalidation_sha256": raw_sha256(evidence.terminal.artifact_raw),
        "user_fix_binding_relative_path": str(
            evidence.binding_path.relative_to(attempt_root)
        ),
        "user_fix_binding_sha256": raw_sha256(evidence.binding_raw),
        "user_fix_intent_relative_path": str(
            evidence.intent_path.relative_to(attempt_root)
        ),
        "user_fix_intent_sha256": raw_sha256(evidence.intent_raw),
    }


def _closed_rejection_context(
    ledger_path: Path,
    ledger: JsonObject,
    user_authorization: Path | None,
    terminal_revalidation: Path | None,
) -> JsonObject:
    stored = ledger.get("rejection_close")
    if not isinstance(stored, dict):
        _fail("closed ledger lacks rejection close context")
    if stored.get("user_fix_intent_sha256") is None:
        if user_authorization is not None or terminal_revalidation is not None:
            _fail("closed non-USER rejection received USER recovery arguments")
        return _ordinary_rejection_close(ledger)
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    binding_relative = _text(
        stored.get("user_fix_binding_relative_path"),
        "USER binding relative path",
    )
    binding_path = attempt_root / binding_relative
    if user_authorization is not None and user_authorization != binding_path:
        _fail("closed USER recovery binding differs from stored context")
    terminal_relative = _text(
        stored.get("terminal_revalidation_relative_path"),
        "terminal relative path",
    )
    terminal_path = attempt_root / terminal_relative
    if terminal_revalidation is not None and terminal_revalidation != terminal_path:
        _fail("closed USER recovery terminal differs from stored context")
    reconstructed = copy.deepcopy(ledger)
    reconstructed["state"] = "open"
    reconstructed["closed_at_utc"] = None
    reconstructed["rejection_close"] = None
    binding, _raw = load_json(binding_path)
    close_terminal = terminal_path if binding.get("boot_changed") is True else None
    return _user_rejection_close(
        ledger_path,
        reconstructed,
        canonical_bytes(reconstructed),
        binding_path,
        close_terminal,
    )


def _sha40(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA40_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
