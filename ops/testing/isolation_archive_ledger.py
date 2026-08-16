"""Validate the closed-ledger authority consumed by archive rollover."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import IsolationError, JsonObject

if TYPE_CHECKING:
    from ops.testing.isolation_archive_contract import ArchivePaths, ArchiveRequest

LEDGER_SCHEMA_VERSION: Final = 2
SHA256_LENGTH: Final = 64
CLOSE_KEYS: Final = {
    "rejection_spec_sha256",
    "terminal_revalidation_relative_path",
    "terminal_revalidation_sha256",
    "user_fix_binding_relative_path",
    "user_fix_binding_sha256",
    "user_fix_intent_relative_path",
    "user_fix_intent_sha256",
}


def validate_closed_archive_ledger(
    paths: ArchivePaths,
    request: ArchiveRequest,
    ledger: JsonObject,
    lock_identity: JsonObject,
    root_keys: frozenset[str],
) -> None:
    """Bind a claim-free closed root and its exact rejection close context."""
    if (
        set(ledger) != root_keys
        or ledger.get("schema_version") != LEDGER_SCHEMA_VERSION
    ):
        _fail("archived ledger root is not the closed version 2 contract")
    if (
        ledger.get("state") != "closed"
        or not isinstance(ledger.get("closed_at_utc"), str)
        or ledger.get("claims") != []
    ):
        _fail("archive requires one claim-free closed ledger")
    if (
        ledger.get("attempt_id") != request.closed_attempt
        or ledger.get("attempt_root") != str(paths.attempt)
        or ledger.get("lock_path") != str(paths.lock)
        or ledger.get("lock_identity") != lock_identity
    ):
        _fail("closed ledger differs from archive authority")
    rejection_close = ledger.get("rejection_close")
    if not isinstance(rejection_close, dict):
        _fail("closed ledger lacks rejection close context")
    if (
        set(rejection_close) != CLOSE_KEYS
        or rejection_close.get("rejection_spec_sha256") != request.rejection_spec_sha256
    ):
        _fail("closed ledger rejection context differs from archive request")
    if rejection_close.get("user_fix_intent_sha256") is None:
        if any(
            rejection_close.get(key) is not None
            for key in CLOSE_KEYS - {"rejection_spec_sha256"}
        ):
            _fail("non-USER archive has partial USER close context")
        return
    boot_id = ledger.get("boot_id")
    expected_user_context = {
        "terminal_revalidation_relative_path": (
            f"terminal-revalidation/{boot_id}.json"
        ),
        "user_fix_binding_relative_path": f"user-fix-bindings/{boot_id}.json",
        "user_fix_intent_relative_path": "user-fix-intent.json",
    }
    if any(
        rejection_close.get(key) != value
        for key, value in expected_user_context.items()
    ) or any(
        not _sha256(rejection_close.get(key))
        for key in (
            "terminal_revalidation_sha256",
            "user_fix_binding_sha256",
            "user_fix_intent_sha256",
        )
    ):
        _fail("USER archive close context is incomplete or noncanonical")


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _fail(message: str) -> Never:
    raise IsolationError(message)
