"""Recover the approved persistent publisher to released and zero claims."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import (
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
    utc_now,
    write_atomic_replace,
)
from ops.testing.isolation_terminal_publisher_authorizations import (
    validate_terminal_publisher_claim,
)
from ops.testing.isolation_terminal_publisher_contract import (
    validate_final_wave_journal,
)
from ops.testing.isolation_terminal_publisher_journal import (
    RELEASE_KEYS,
    validate_initial_publisher_journal,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.testing.isolation_ledger_store import LedgerSession


@dataclass(frozen=True, slots=True)
class _ReleaseIntentInputs:
    journal: JsonObject
    ledger: JsonObject
    ledger_raw: bytes
    current_boot: str
    final_path: Path
    authorization_sha: str


def release_approved_terminal_publisher(
    session: LedgerSession,
    current_boot: str,
    final_path: Path,
    emit: Callable[[str, JsonObject], None],
) -> None:
    """Replay active or release-intent publisher state to released."""
    ledger = session.ledger
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    path = attempt_root / "final-wave-state.json"
    regular_identity(path, mode=MODE_PRIVATE)
    journal, _raw = load_json(path)
    _validate_journal_root(journal, ledger, final_path)
    state = journal.get("terminal_publisher_state")
    if state == "released":
        if ledger.get("claims") != []:
            _fail("released terminal publisher remains in the ledger")
        return
    if state == "active":
        claim = _sole_active_publisher(ledger)
        validate_initial_publisher_journal(ledger, claim, journal)
        _require_claim_root_absent(attempt_root, claim)
        authorization_sha = validate_terminal_publisher_claim(
            claim,
            final_path.parent / "terminal",
        )
        journal = _release_intent(
            _ReleaseIntentInputs(
                journal,
                ledger,
                session.original_raw,
                current_boot,
                final_path,
                authorization_sha,
            )
        )
        write_atomic_replace(path, canonical_bytes(journal))
        journal, _raw = load_json(path)
        emit("publisher-release-intent", journal)
    elif state != "release-intent":
        _fail("terminal publisher is not active, release-intent, or released")
    pre_sha = _text(
        journal.get("terminal_publisher_pre_release_ledger_sha256"),
        "publisher pre-release ledger SHA",
    )
    post_sha = _text(
        journal.get("terminal_publisher_post_release_ledger_sha256"),
        "publisher post-release ledger SHA",
    )
    current_sha = raw_sha256(session.original_raw)
    if current_sha == pre_sha:
        claim = _sole_active_publisher(ledger)
        _validate_release_intent(journal, ledger, claim, current_boot, final_path)
        post = copy.deepcopy(ledger)
        post["claims"] = []
        if raw_sha256(canonical_bytes(post)) != post_sha:
            _fail("publisher release intent binds different post-ledger bytes")
        session.ledger.clear()
        session.ledger.update(post)
        session.commit()
        emit("publisher-ledger-updated", journal)
    elif current_sha != post_sha or ledger.get("claims") != []:
        _fail("publisher release ledger is neither its pre nor post image")
    released = copy.deepcopy(journal)
    released["terminal_publisher_state"] = "released"
    released["terminal_publisher_released_at_utc"] = utc_now()
    released["updated_at_utc"] = released["terminal_publisher_released_at_utc"]
    write_atomic_replace(path, canonical_bytes(released))
    observed, _raw = load_json(path)
    emit("publisher-released", observed)


def _release_intent(inputs: _ReleaseIntentInputs) -> JsonObject:
    final, _raw = load_json(inputs.final_path)
    post = copy.deepcopy(inputs.ledger)
    post["claims"] = []
    result = copy.deepcopy(inputs.journal)
    result["terminal_publisher_release_kind"] = "approved-chain"
    result["terminal_publisher_release_context"] = (
        "same-boot"
        if inputs.ledger.get("boot_id") == inputs.current_boot
        else "stale-boot"
    )
    result["terminal_publisher_release_boot_id"] = inputs.current_boot
    result["terminal_publisher_release_basis_sha256"] = final.get("approvals_sha256")
    result["terminal_publisher_release_authorizations_sha256"] = (
        inputs.authorization_sha
    )
    result["terminal_publisher_pre_release_ledger_sha256"] = raw_sha256(
        inputs.ledger_raw
    )
    result["terminal_publisher_post_release_ledger_sha256"] = raw_sha256(
        canonical_bytes(post)
    )
    result["terminal_publisher_released_at_utc"] = None
    result["terminal_publisher_state"] = "release-intent"
    result["updated_at_utc"] = utc_now()
    return result


def _validate_release_intent(
    journal: JsonObject,
    ledger: JsonObject,
    claim: JsonObject,
    current_boot: str,
    final_path: Path,
) -> None:
    initial = copy.deepcopy(journal)
    initial["terminal_publisher_state"] = "active"
    for key in RELEASE_KEYS:
        initial[key] = None
    validate_initial_publisher_journal(ledger, claim, initial)
    authorization_sha = validate_terminal_publisher_claim(
        claim,
        final_path.parent / "terminal",
    )
    final, _raw = load_json(final_path)
    expected = {
        "terminal_publisher_release_authorizations_sha256": authorization_sha,
        "terminal_publisher_release_basis_sha256": final.get("approvals_sha256"),
        "terminal_publisher_release_boot_id": current_boot,
        "terminal_publisher_release_context": (
            "same-boot" if ledger.get("boot_id") == current_boot else "stale-boot"
        ),
        "terminal_publisher_release_kind": "approved-chain",
        "terminal_publisher_released_at_utc": None,
    }
    if any(journal.get(key) != value for key, value in expected.items()):
        _fail("approved publisher release intent fields drifted")


def _validate_journal_root(
    journal: JsonObject,
    ledger: JsonObject,
    final_path: Path,
) -> None:
    validate_final_wave_journal(journal)
    if (
        journal.get("attempt_id") != ledger.get("attempt_id")
        or journal.get("phase") != "final-frozen"
        or journal.get("control_root") != str(final_path.parent)
    ):
        _fail("terminal publisher journal is not the sealed approval authority")


def _sole_active_publisher(ledger: JsonObject) -> JsonObject:
    claims = ledger.get("claims")
    if not isinstance(claims, list) or len(claims) != 1:
        _fail("terminal release lacks its sole publisher claim")
    claim = claims[0]
    if not isinstance(claim, dict) or claim.get("status") != "active":
        _fail("terminal release claim is not the active publisher")
    return claim


def _require_claim_root_absent(attempt_root: Path, claim: JsonObject) -> None:
    relative = _text(claim.get("root_relative_path"), "publisher claim root")
    path = attempt_root / relative
    if path.exists() or path.is_symlink():
        _fail("approved publisher staging root remains before release")


def _text(value: object, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
