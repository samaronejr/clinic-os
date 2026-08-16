"""Validate the closed final-wave journal schema and state matrix."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Never

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

JOURNAL_KEYS: Final = {
    "attempt_id",
    "bootstrap_root",
    "control_root",
    "control_root_identity",
    "f4_sha256",
    "final_sha256",
    "inputs_sha256",
    "lineage_validation_sha256",
    "lock_identity",
    "phase",
    "pre_f4_sha256",
    "receipt_lineage_sha256",
    "schema_version",
    "sha",
    "terminal_publisher_claim_id",
    "terminal_publisher_post_release_ledger_sha256",
    "terminal_publisher_post_reservation_ledger_sha256",
    "terminal_publisher_pre_release_ledger_sha256",
    "terminal_publisher_pre_reservation_ledger_sha256",
    "terminal_publisher_release_authorizations_sha256",
    "terminal_publisher_release_basis_sha256",
    "terminal_publisher_release_boot_id",
    "terminal_publisher_release_context",
    "terminal_publisher_release_kind",
    "terminal_publisher_released_at_utc",
    "terminal_publisher_reservation_at_utc",
    "terminal_publisher_reservation_spec_sha256",
    "terminal_publisher_state",
    "updated_at_utc",
}
RELEASE_KEYS: Final = (
    "terminal_publisher_release_kind",
    "terminal_publisher_release_context",
    "terminal_publisher_release_boot_id",
    "terminal_publisher_release_basis_sha256",
    "terminal_publisher_release_authorizations_sha256",
    "terminal_publisher_pre_release_ledger_sha256",
    "terminal_publisher_post_release_ledger_sha256",
    "terminal_publisher_released_at_utc",
)
RESERVATION_KEYS: Final = (
    "terminal_publisher_reservation_spec_sha256",
    "terminal_publisher_pre_reservation_ledger_sha256",
    "terminal_publisher_post_reservation_ledger_sha256",
)
PHASES: Final = {
    "initializing",
    "inputs-frozen",
    "lanes-complete",
    "pre-f4-frozen",
    "f4-rejected",
    "f4-created",
    "final-frozen",
    "rejected",
}
STATES: Final = {
    "unbound",
    "id-bound",
    "reserved",
    "active",
    "release-intent",
    "released",
}
MIDDLE_PHASES: Final = {
    "inputs-frozen",
    "lanes-complete",
    "pre-f4-frozen",
    "f4-created",
}
TERMINAL_PHASES: Final = {"f4-rejected", "rejected", "final-frozen"}
IDENTITY_KEYS: Final = {"device", "gid", "inode", "mode", "uid"}
SHA40: Final = re.compile(r"^[0-9a-f]{40}$")
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
UUID: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
TIMESTAMP: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)


def validate_final_wave_journal(journal: JsonObject) -> None:
    """Require exact fields, scalar formats, and publisher phase coupling."""
    if set(journal) != JOURNAL_KEYS or journal.get("schema_version") != 1:
        _fail("final-wave publisher journal has an open or unknown root")
    _match(journal.get("attempt_id"), UUID, "attempt UUID")
    _match(journal.get("sha"), SHA40, "source SHA")
    _match(journal.get("updated_at_utc"), TIMESTAMP, "update timestamp")
    _absolute(journal.get("bootstrap_root"), "bootstrap root")
    _absolute(journal.get("control_root"), "control root")
    _identity(journal.get("control_root_identity"), "control root identity")
    _identity(journal.get("lock_identity"), "final lock identity")
    for key in ("lineage_validation_sha256", "receipt_lineage_sha256"):
        _match(journal.get(key), SHA256, key)
    for key in ("inputs_sha256", "pre_f4_sha256", "f4_sha256", "final_sha256"):
        _nullable_match(journal.get(key), SHA256, key)
    phase = journal.get("phase")
    state = journal.get("terminal_publisher_state")
    if (
        not isinstance(phase, str)
        or phase not in PHASES
        or not isinstance(state, str)
        or state not in STATES
    ):
        _fail("final-wave phase or publisher state is unknown")
    _validate_phase_state(phase, state)
    _validate_reservation(journal, state)
    _validate_release(journal, state)
    _validate_phase_hashes(journal, phase)


def _validate_phase_state(phase: str, state: str) -> None:
    if phase in MIDDLE_PHASES and state != "active":
        _fail("final-wave phase/state matrix requires an active publisher")
    if phase in TERMINAL_PHASES and state not in {
        "active",
        "release-intent",
        "released",
    }:
        _fail("final-wave terminal phase/state matrix is invalid")
    if state in {"unbound", "id-bound", "reserved"} and phase != "initializing":
        _fail("final-wave publisher state is invalid for this phase")


def _validate_reservation(journal: JsonObject, state: str) -> None:
    claim_id = journal.get("terminal_publisher_claim_id")
    timestamp = journal.get("terminal_publisher_reservation_at_utc")
    if state == "unbound":
        if (
            claim_id is not None
            or timestamp is not None
            or any(journal.get(key) is not None for key in RESERVATION_KEYS)
        ):
            _fail("unbound final-wave journal carries reservation fields")
        return
    _match(claim_id, UUID, "publisher claim UUID")
    _match(timestamp, TIMESTAMP, "publisher reservation timestamp")
    for key in RESERVATION_KEYS:
        _match(journal.get(key), SHA256, key)


def _validate_release(journal: JsonObject, state: str) -> None:
    if state not in {"release-intent", "released"}:
        if any(journal.get(key) is not None for key in RELEASE_KEYS):
            _fail("unreleased final-wave journal carries release fields")
        return
    release_kind = journal.get("terminal_publisher_release_kind")
    release_context = journal.get("terminal_publisher_release_context")
    if (
        not isinstance(release_kind, str)
        or release_kind not in {"approved-chain", "rejection-prefix"}
        or not isinstance(release_context, str)
        or release_context not in {"same-boot", "stale-boot"}
    ):
        _fail("publisher release kind or context is invalid")
    _match(
        journal.get("terminal_publisher_release_boot_id"),
        UUID,
        "publisher release boot UUID",
    )
    for key in (
        "terminal_publisher_release_basis_sha256",
        "terminal_publisher_release_authorizations_sha256",
        "terminal_publisher_pre_release_ledger_sha256",
        "terminal_publisher_post_release_ledger_sha256",
    ):
        _match(journal.get(key), SHA256, key)
    released = journal.get("terminal_publisher_released_at_utc")
    if state == "release-intent" and released is not None:
        _fail("publisher release intent already has a release timestamp")
    if state == "released":
        _match(released, TIMESTAMP, "publisher released timestamp")


def _validate_phase_hashes(journal: JsonObject, phase: str) -> None:
    required: tuple[str, ...] = ()
    if phase in {"inputs-frozen", "lanes-complete"}:
        required = ("inputs_sha256",)
    elif phase == "pre-f4-frozen":
        required = ("inputs_sha256", "pre_f4_sha256")
    elif phase in {"f4-created", "f4-rejected"}:
        required = ("inputs_sha256", "pre_f4_sha256", "f4_sha256")
    elif phase == "final-frozen":
        required = (
            "inputs_sha256",
            "pre_f4_sha256",
            "f4_sha256",
            "final_sha256",
        )
    if any(journal.get(key) is None for key in required):
        _fail("final-wave phase lacks its required immutable hash")


def _identity(value: JsonValue, context: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) != IDENTITY_KEYS
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in value.values()
        )
    ):
        _fail(f"{context} is not the closed identity object")


def _absolute(value: JsonValue, context: str) -> None:
    if not isinstance(value, str) or not Path(value).is_absolute():
        _fail(f"{context} is not absolute")


def _nullable_match(value: JsonValue, pattern: re.Pattern[str], context: str) -> None:
    if value is not None:
        _match(value, pattern, context)


def _match(value: JsonValue, pattern: re.Pattern[str], context: str) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _fail(f"{context} has an invalid format")


def _fail(message: str) -> Never:
    raise IsolationError(message)
