"""Record immutable receipt-authenticated rejection specifications."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never, cast

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
    write_no_replace,
)
from ops.testing.isolation_failure_receipts import load_failure_receipts
from ops.testing.isolation_ledger_store import locked_open_ledger
from ops.testing.isolation_quiescence import require_quiescent_attempt
from ops.testing.isolation_rejection_evidence import phase_hashes, receipt_rejections
from ops.testing.isolation_user_fix import (
    UserFixValidationInputs,
    validate_user_fix_authorization,
)

if TYPE_CHECKING:
    from collections.abc import Callable


SHA40_LENGTH = 40
SHA256_LENGTH = 64


@dataclass(frozen=True, slots=True)
class RejectionRequest:
    """Closed caller inputs that must match authenticated attempt evidence."""

    sha: str
    reasons: tuple[str, ...]
    inputs_sha256: str | None
    pre_f4_sha256: str | None
    final_sha256: str | None
    user_authorization: Path | None = None


def build_rejection_context(
    ledger_path: Path,
    *,
    sha: str,
    control_root: Path,
    inventory_reader: Callable[[], JsonObject],
) -> tuple[str, ...]:
    """Project the fixed rejection command context without mutating authority."""
    _require_sha(sha, SHA40_LENGTH, "rejected SHA")
    with locked_open_ledger(ledger_path) as session:
        require_quiescent_attempt(ledger_path, session.ledger, inventory_reader)
        phase_values = phase_hashes(ledger_path, control_root)
        _rejections, _aggregate, reasons, _retry = receipt_rejections(
            session.ledger,
            sha,
        )
        return _context_fields(session.ledger, phase_values, reasons)


def reject_attempt(
    ledger_path: Path,
    request: RejectionRequest,
    *,
    inventory_reader: Callable[[], JsonObject],
) -> str:
    """Publish or validate one immutable rejection specification."""
    _validate_request(request)
    with locked_open_ledger(ledger_path) as session:
        require_quiescent_attempt(ledger_path, session.ledger, inventory_reader)
        control_root = ledger_path.parent / "clinic-os-phase1a-final"
        observed_phases = phase_hashes(ledger_path, control_root)
        requested_phases = (
            request.inputs_sha256,
            request.pre_f4_sha256,
            request.final_sha256,
        )
        if requested_phases != observed_phases:
            _fail("rejection phase hashes differ from immutable artifacts")
        aggregate: str | None
        user_intent_sha: str | None
        if request.user_authorization is None:
            rejections, aggregate, reasons, retry_kind = receipt_rejections(
                session.ledger,
                request.sha,
            )
            user_intent_sha = None
            if request.reasons != reasons:
                _fail("rejection reasons differ from authenticated receipts")
        else:
            evidence = validate_user_fix_authorization(
                UserFixValidationInputs(
                    ledger_path,
                    session.ledger,
                    session.original_raw,
                    request.sha,
                    request.user_authorization,
                    control_root,
                )
            )
            receipts, aggregate = load_failure_receipts(session.ledger)
            if receipts or aggregate is not None:
                _fail("USER rejection cannot coexist with failure receipts")
            if request.reasons != ("USER:user-requested-fix",):
                _fail("USER rejection reason differs from its authorization")
            rejections = [{"failure_class": "user-requested-fix", "lane": "USER"}]
            retry_kind = "source-fix"
            user_intent_sha = raw_sha256(evidence.intent_raw)
        rejection_values = cast("list[JsonValue]", rejections)
        spec: JsonObject = {
            "attempt_id": session.ledger["attempt_id"],
            "failure_receipts_sha256": aggregate,
            "final_sha256": request.final_sha256,
            "inputs_sha256": request.inputs_sha256,
            "pre_f4_sha256": request.pre_f4_sha256,
            "rejections": rejection_values,
            "retry_kind": retry_kind,
            "schema_version": 1,
            "sha": request.sha,
            "user_fix_intent_sha256": user_intent_sha,
        }
        return _publish_spec(session.ledger, spec)


def _context_fields(
    ledger: JsonObject,
    phases: tuple[str | None, ...],
    reasons: tuple[str, ...],
) -> tuple[str, ...]:
    binding = _object(ledger.get("authority_binding"), "authority binding")
    plan = _object(ledger.get("approved_plan"), "approved plan")
    fixed = (
        _text(ledger.get("attempt_id"), "attempt ID"),
        *(_none_or_hash(value) for value in phases),
        _text(binding.get("authority_workspace_realpath"), "authority workspace"),
        _text(binding.get("authority_root_realpath"), "authority root"),
        _text(plan.get("path"), "approved plan path"),
        _text(ledger.get("worktree_realpath"), "feature worktree"),
        "none",
    )
    return (*fixed, *reasons)


def _publish_spec(ledger: JsonObject, spec: JsonObject) -> str:
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    path = attempt_root / "rejection-spec.json"
    expected = canonical_bytes(spec)
    try:
        write_no_replace(path, expected, mode=MODE_IMMUTABLE)
    except FileExistsError:
        regular_identity(path, mode=MODE_IMMUTABLE)
        _existing, raw = load_json(path)
        if raw != expected:
            _fail("existing rejection specification differs")
    return raw_sha256(expected)


def _validate_request(request: RejectionRequest) -> None:
    _require_sha(request.sha, SHA40_LENGTH, "rejected SHA")
    if not request.reasons or request.reasons != tuple(sorted(set(request.reasons))):
        _fail("rejection reasons must be a nonempty sorted unique set")
    if request.user_authorization is not None and request.reasons != (
        "USER:user-requested-fix",
    ):
        _fail("USER authorization requires the sole USER repair reason")
    for value, label in (
        (request.inputs_sha256, "inputs SHA"),
        (request.pre_f4_sha256, "pre-F4 SHA"),
        (request.final_sha256, "final SHA"),
    ):
        if value is not None:
            _require_sha(value, SHA256_LENGTH, label)


def _require_sha(value: str, length: int, context: str) -> None:
    if len(value) != length or any(
        character not in "0123456789abcdef" for character in value
    ):
        _fail(f"{context} must be lowercase {length}-hex")


def _none_or_hash(value: str | None) -> str:
    return "none" if value is None else value


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
