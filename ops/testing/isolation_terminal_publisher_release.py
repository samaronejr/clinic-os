"""Journal stale-boot rejection release of the persistent publisher."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import TYPE_CHECKING, Never, cast

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    raw_sha256,
    utc_now,
    write_atomic_replace,
)
from ops.testing.isolation_terminal_publisher_authorizations import (
    validate_terminal_publisher_claim,
)
from ops.testing.isolation_terminal_publisher_journal import (
    RELEASE_KEYS,
    load_terminal_publisher_journal,
    validate_initial_publisher_journal,
)

if TYPE_CHECKING:
    from collections.abc import Callable

type Checkpoint = Callable[[str, JsonObject], None]


def prepare_publisher_rejection_release(
    ledger: JsonObject,
    outer: JsonObject,
    emit: Checkpoint,
) -> str:
    """Fsync or adopt release intent before binding it in the outer journal."""
    path = _journal_path(outer)
    publisher, _claims = _publisher_claim(ledger, outer)
    current, raw = load_terminal_publisher_journal(path)
    terminal_root = Path(_text(current.get("control_root"), "control root"))
    terminal_root /= "terminal"
    authorizations_sha256 = validate_terminal_publisher_claim(publisher, terminal_root)
    _require_claim_root_absent(ledger, publisher)
    if current.get("terminal_publisher_state") in {"reserved", "active"}:
        validate_initial_publisher_journal(ledger, publisher, current)
        if raw_sha256(raw) != outer.get("publisher_initial_journal_sha256"):
            _fail("publisher initial journal differs from outer authority")
        current = _release_intent(current, outer, authorizations_sha256)
        write_atomic_replace(path, canonical_bytes(current))
        current, raw = load_terminal_publisher_journal(path)
    elif current.get("terminal_publisher_state") == "release-intent":
        _validate_release_intent(current, outer, authorizations_sha256)
        initial = _initial_prefix(current, str(publisher.get("status")))
        if raw_sha256(canonical_bytes(initial)) != outer.get(
            "publisher_initial_journal_sha256"
        ):
            _fail("publisher release intent cannot reconstruct its initial journal")
    else:
        _fail("publisher is neither initial nor at rejection release intent")
    _validate_release_intent(current, outer, authorizations_sha256)
    emit("publisher-release-intent-applied", outer)
    return raw_sha256(raw)


def finish_publisher_rejection_release(outer: JsonObject) -> str:
    """Seal released only after the outer journal acknowledges the boot update."""
    path = _journal_path(outer)
    current, raw = load_terminal_publisher_journal(path)
    if current.get("terminal_publisher_state") == "release-intent":
        if raw_sha256(raw) != outer.get("publisher_release_intent_sha256"):
            _fail("publisher release intent hash differs from outer authority")
        current = copy.deepcopy(current)
        current["terminal_publisher_state"] = "released"
        current["terminal_publisher_released_at_utc"] = utc_now()
        write_atomic_replace(path, canonical_bytes(current))
        current, raw = load_terminal_publisher_journal(path)
    elif current.get("terminal_publisher_state") == "released":
        intent = copy.deepcopy(current)
        intent["terminal_publisher_state"] = "release-intent"
        intent["terminal_publisher_released_at_utc"] = None
        if raw_sha256(canonical_bytes(intent)) != outer.get(
            "publisher_release_intent_sha256"
        ):
            _fail("released publisher cannot reconstruct its bound intent")
    else:
        _fail("publisher journal is not at release intent or released")
    _validate_released(current, outer)
    return raw_sha256(raw)


def _release_intent(
    journal: JsonObject,
    outer: JsonObject,
    authorizations_sha256: str,
) -> JsonObject:
    result = copy.deepcopy(journal)
    result["terminal_publisher_release_kind"] = "rejection-prefix"
    result["terminal_publisher_release_context"] = "stale-boot"
    result["terminal_publisher_release_boot_id"] = outer.get("current_boot_id")
    result["terminal_publisher_release_basis_sha256"] = outer.get(
        "failure_receipts_sha256"
    )
    result["terminal_publisher_release_authorizations_sha256"] = authorizations_sha256
    result["terminal_publisher_pre_release_ledger_sha256"] = outer.get(
        "post_cleanup_ledger_sha256"
    )
    result["terminal_publisher_post_release_ledger_sha256"] = outer.get(
        "post_update_ledger_sha256"
    )
    result["terminal_publisher_released_at_utc"] = None
    result["terminal_publisher_state"] = "release-intent"
    return result


def _validate_release_intent(
    journal: JsonObject,
    outer: JsonObject,
    authorizations_sha256: str,
) -> None:
    expected = {
        "terminal_publisher_post_release_ledger_sha256": outer.get(
            "post_update_ledger_sha256"
        ),
        "terminal_publisher_pre_release_ledger_sha256": outer.get(
            "post_cleanup_ledger_sha256"
        ),
        "terminal_publisher_release_authorizations_sha256": authorizations_sha256,
        "terminal_publisher_release_basis_sha256": outer.get("failure_receipts_sha256"),
        "terminal_publisher_release_boot_id": outer.get("current_boot_id"),
        "terminal_publisher_release_context": "stale-boot",
        "terminal_publisher_release_kind": "rejection-prefix",
        "terminal_publisher_released_at_utc": None,
    }
    if journal.get("terminal_publisher_state") != "release-intent" or any(
        journal.get(key) != value for key, value in expected.items()
    ):
        _fail("publisher rejection release intent fields drifted")


def _validate_released(journal: JsonObject, outer: JsonObject) -> None:
    if journal.get("terminal_publisher_state") != "released":
        _fail("publisher final journal is not released")
    if not isinstance(journal.get("terminal_publisher_released_at_utc"), str):
        _fail("publisher released journal lacks its terminal timestamp")
    expected = {
        "terminal_publisher_post_release_ledger_sha256": outer.get(
            "post_update_ledger_sha256"
        ),
        "terminal_publisher_pre_release_ledger_sha256": outer.get(
            "post_cleanup_ledger_sha256"
        ),
        "terminal_publisher_release_basis_sha256": outer.get("failure_receipts_sha256"),
        "terminal_publisher_release_boot_id": outer.get("current_boot_id"),
        "terminal_publisher_release_context": "stale-boot",
        "terminal_publisher_release_kind": "rejection-prefix",
    }
    if any(journal.get(key) != value for key, value in expected.items()):
        _fail("publisher released journal fields drifted")


def _initial_prefix(journal: JsonObject, status: str) -> JsonObject:
    if status not in {"reserved", "active"}:
        _fail("publisher claim status cannot reconstruct initial journal")
    result = copy.deepcopy(journal)
    result["terminal_publisher_state"] = status
    for key in RELEASE_KEYS:
        result[key] = None
    return result


def _publisher_claim(
    ledger: JsonObject,
    outer: JsonObject,
) -> tuple[JsonObject, list[JsonObject]]:
    claims = _objects(ledger.get("claims"), "ledger claims")
    matches = [
        item
        for item in claims
        if item.get("claim_id") == outer.get("publisher_claim_id")
        and item.get("purpose") == "final-terminal-publisher"
    ]
    if len(matches) != 1 or len(claims) != 1:
        _fail("post-cleanup ledger lacks its sole bound publisher")
    return matches[0], claims


def _require_claim_root_absent(ledger: JsonObject, publisher: JsonObject) -> None:
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    root = attempt_root / _text(publisher.get("root_relative_path"), "claim root")
    try:
        os.lstat(root)
    except FileNotFoundError:
        return
    _fail("publisher claim root remains before rejection release intent")


def _journal_path(outer: JsonObject) -> Path:
    value = _text(
        outer.get("publisher_final_wave_journal_path"), "publisher journal path"
    )
    path = Path(value)
    if not path.is_absolute():
        _fail("publisher journal path is not absolute")
    return path


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
