"""Build the exact rejection tombstone from immutable archived evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, cast

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
)
from ops.testing.isolation_failure_receipts import load_failure_receipts
from ops.testing.isolation_lineage import LineageHashes, load_complete_lineage
from ops.testing.isolation_rejection_evidence import receipt_rejections

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_archive_contract import ArchiveRequest

SPEC_KEYS: Final = {
    "schema_version",
    "attempt_id",
    "sha",
    "retry_kind",
    "rejections",
    "failure_receipts_sha256",
    "user_fix_intent_sha256",
    "inputs_sha256",
    "pre_f4_sha256",
    "final_sha256",
}
TOMBSTONE_KEYS: Final = {
    "schema_version",
    "attempt_id",
    "sha",
    "retry_kind",
    "rejections",
    "rejecting_lanes",
    "failure_receipts_sha256",
    "user_fix_intent_sha256",
    "user_fix_binding_sha256",
    "terminal_revalidation_sha256",
    "inputs_sha256",
    "pre_f4_sha256",
    "final_sha256",
    "control_tree_sha256",
    "attempt_tree_sha256",
    "receipt_lineage_seed_sha256",
    "lineage_validation_sha256",
    "receipt_lineage_sha256",
    "rejection_spec_sha256",
    "ledger_sha256",
}


@dataclass(frozen=True, slots=True)
class ArchiveContentHashes:
    """Content hashes copied into the rejection tombstone."""

    control_sha256: str
    attempt_sha256: str
    ledger_sha256: str


def build_archive_tombstone(
    ledger: JsonObject,
    attempt_path: Path,
    request: ArchiveRequest,
    hashes: ArchiveContentHashes,
) -> tuple[JsonObject, bytes]:
    """Authenticate spec, receipts, lineage, and build their closed tombstone."""
    spec, spec_raw = _load_spec(attempt_path, request, ledger)
    user_hashes = _user_hashes(ledger, attempt_path, spec)
    if user_hashes is None:
        rejections, aggregate, _reasons, retry_kind = receipt_rejections(
            ledger,
            request.rejected_sha,
            attempt_path,
        )
        user_intent_sha, user_binding_sha, terminal_sha = (None, None, None)
    else:
        rejections = [{"failure_class": "user-requested-fix", "lane": "USER"}]
        aggregate = None
        retry_kind = "source-fix"
        user_intent_sha, user_binding_sha, terminal_sha = user_hashes
    if (
        spec.get("rejections") != rejections
        or spec.get("failure_receipts_sha256") != aggregate
        or spec.get("retry_kind") != retry_kind
    ):
        _fail("rejection specification differs from authenticated receipts")
    lineage = load_complete_lineage(attempt_path, request.closed_attempt)
    _validate_receipt_bindings(
        ledger,
        attempt_path,
        hashes.control_sha256,
        lineage,
    )
    rejection_values = cast("list[JsonValue]", rejections)
    lanes = cast(
        "list[JsonValue]",
        sorted({str(rejection["lane"]) for rejection in rejections}),
    )
    tombstone: JsonObject = {
        "attempt_id": request.closed_attempt,
        "attempt_tree_sha256": hashes.attempt_sha256,
        "control_tree_sha256": hashes.control_sha256,
        "failure_receipts_sha256": aggregate,
        "final_sha256": spec.get("final_sha256"),
        "inputs_sha256": spec.get("inputs_sha256"),
        "ledger_sha256": hashes.ledger_sha256,
        "lineage_validation_sha256": lineage.validation_sha256,
        "pre_f4_sha256": spec.get("pre_f4_sha256"),
        "receipt_lineage_seed_sha256": lineage.seed_sha256,
        "receipt_lineage_sha256": lineage.lineage_sha256,
        "rejecting_lanes": lanes,
        "rejection_spec_sha256": raw_sha256(spec_raw),
        "rejections": rejection_values,
        "retry_kind": spec.get("retry_kind"),
        "schema_version": 1,
        "sha": request.rejected_sha,
        "terminal_revalidation_sha256": terminal_sha,
        "user_fix_binding_sha256": user_binding_sha,
        "user_fix_intent_sha256": user_intent_sha,
    }
    if set(tombstone) != TOMBSTONE_KEYS:
        _fail("internal rejection tombstone root drifted")
    return tombstone, canonical_bytes(tombstone)


def _load_spec(
    attempt_path: Path,
    request: ArchiveRequest,
    ledger: JsonObject,
) -> tuple[JsonObject, bytes]:
    path = attempt_path / "rejection-spec.json"
    regular_identity(path, mode=MODE_IMMUTABLE)
    spec, raw = load_json(path)
    if set(spec) != SPEC_KEYS or spec.get("schema_version") != 1:
        _fail("rejection specification has an open or unknown root")
    if (
        raw_sha256(raw) != request.rejection_spec_sha256
        or spec.get("attempt_id") != request.closed_attempt
        or spec.get("sha") != request.rejected_sha
        or ledger.get("rejection_close") is None
    ):
        _fail("rejection specification differs from archive authority")
    close = cast("JsonObject", ledger["rejection_close"])
    if spec.get("user_fix_intent_sha256") != close.get("user_fix_intent_sha256"):
        _fail("rejection specification USER intent differs from close context")
    return spec, raw


def _user_hashes(
    ledger: JsonObject,
    attempt_path: Path,
    spec: JsonObject,
) -> tuple[str, str, str] | None:
    intent_sha = spec.get("user_fix_intent_sha256")
    if intent_sha is None:
        return None
    close = ledger.get("rejection_close")
    if not isinstance(close, dict):
        _fail("USER archive lacks rejection close context")
    receipts, aggregate = load_failure_receipts(ledger, attempt_path)
    if receipts or aggregate is not None:
        _fail("USER archive cannot contain failure receipts")
    values = (
        _artifact_hash(
            attempt_path,
            close,
            "user_fix_intent_relative_path",
            "user_fix_intent_sha256",
        ),
        _artifact_hash(
            attempt_path,
            close,
            "user_fix_binding_relative_path",
            "user_fix_binding_sha256",
        ),
        _artifact_hash(
            attempt_path,
            close,
            "terminal_revalidation_relative_path",
            "terminal_revalidation_sha256",
        ),
    )
    if intent_sha != values[0]:
        _fail("USER intent hash differs between specification and archive")
    return values


def _artifact_hash(
    attempt_path: Path,
    close: JsonObject,
    relative_key: str,
    hash_key: str,
) -> str:
    relative = close.get(relative_key)
    expected = close.get(hash_key)
    if not isinstance(relative, str) or not isinstance(expected, str):
        _fail("USER archive close context is incomplete")
    path = attempt_path / relative
    regular_identity(path, mode=MODE_IMMUTABLE)
    if raw_sha256(path.read_bytes()) != expected:
        _fail("USER archive artifact differs from close context")
    return expected


def _validate_receipt_bindings(
    ledger: JsonObject,
    attempt_path: Path,
    control_sha: str,
    lineage: LineageHashes,
) -> None:
    receipts, _aggregate = load_failure_receipts(ledger, attempt_path)
    if any(receipt.get("control_tree_sha256") != control_sha for receipt in receipts):
        _fail("failure receipt control tree differs from archive source")
    if any(
        receipt.get("lineage_validation_sha256") != lineage.validation_sha256
        for receipt in receipts
    ):
        _fail("failure receipt lineage checkpoint differs from archive source")


def _fail(message: str) -> Never:
    raise IsolationError(message)
