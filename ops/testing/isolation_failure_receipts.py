"""Authenticate immutable common failure receipts and their canonical aggregate."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Never, cast

import rfc8785

from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    directory_identity,
    load_json,
    raw_sha256,
    regular_identity,
)

RECEIPT_ROOT_NAME: Final = "final-failure-receipts"
LANES: Final = ("F1", "F2", "F3", "F4", "ORCH")
ROOT_KEYS: Final = {
    "attempt_id",
    "cause_code",
    "cleanup_verified",
    "control_tree_sha256",
    "ended_at_utc",
    "exit_code",
    "failure_class",
    "journal_sha256",
    "lane",
    "lineage_validation_sha256",
    "observed_causes",
    "output_sha256",
    "schema_version",
    "sha",
    "signal",
    "stage",
    "started_at_utc",
    "timed_out",
}
CAUSES: Final = (
    "reviewer-reject",
    "product-assertion",
    "auth-rejected",
    "malformed-verdict",
    "malformed-receipt",
    "deadline-exceeded",
    "session-reused",
    "session-disconnected",
    "session-missing",
    "auth-unavailable",
    "launcher-unavailable",
    "dependency-unavailable",
    "io-failure",
    "unexpected-signal",
    "boot-changed",
    "cleanup-failure",
)
CAUSE_CLASS: Final = {
    "reviewer-reject": "finding",
    "product-assertion": "product-failure",
    "auth-rejected": "authentication",
    "malformed-verdict": "malformed-output",
    "malformed-receipt": "malformed-output",
    "deadline-exceeded": "timeout",
    "session-reused": "session",
    "session-disconnected": "session",
    "session-missing": "session",
    "auth-unavailable": "authentication",
    "launcher-unavailable": "infrastructure",
    "dependency-unavailable": "infrastructure",
    "io-failure": "infrastructure",
    "unexpected-signal": "infrastructure",
    "boot-changed": "infrastructure",
    "cleanup-failure": "infrastructure",
}
STAGES: Final = {
    "F1": {"F1-review"},
    "F2": {"F2-prerequisites", "F2-review"},
    "F3": {"F3-e2e"},
    "F4": {"F4-precheck", "F4-final"},
    "ORCH": {"ORCH-input-freeze", "ORCH-pre-f4-freeze", "ORCH-final-gate"},
}
TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def load_failure_receipts(
    ledger: JsonObject,
    attempt_root_override: Path | None = None,
) -> tuple[list[JsonObject], str | None]:
    """Validate the fixed receipt namespace and return its RFC-8785 aggregate."""
    attempt_root = _absolute_text(ledger.get("attempt_root"), "attempt root")
    attempt_path = (
        Path(attempt_root) if attempt_root_override is None else attempt_root_override
    )
    root = attempt_path / RECEIPT_ROOT_NAME
    identity = directory_identity(root)
    if identity.get("mode") != MODE_DIRECTORY or root.resolve(strict=True) != root:
        _fail("failure receipt root is not the fixed private directory")
    names = sorted(entry.name for entry in root.iterdir())
    allowed = {f"{lane}.json" for lane in LANES}
    if any(name not in allowed for name in names):
        _fail("failure receipt root contains an unknown entry")
    receipts: list[JsonObject] = []
    aggregate_entries: list[JsonObject] = []
    for name in names:
        path = root / name
        regular_identity(path, mode=MODE_IMMUTABLE)
        receipt, raw = load_json(path)
        lane = name.removesuffix(".json")
        _validate_receipt(receipt, ledger, lane)
        receipts.append(receipt)
        aggregate_entries.append(
            {
                "lane": lane,
                "relative_path": f"{RECEIPT_ROOT_NAME}/{name}",
                "sha256": raw_sha256(raw),
            }
        )
    if not aggregate_entries:
        return receipts, None
    return receipts, raw_sha256(rfc8785.dumps(aggregate_entries))


def _validate_receipt(receipt: JsonObject, ledger: JsonObject, lane: str) -> None:
    _validate_receipt_identity(receipt, ledger, lane)
    _validate_receipt_timing(receipt)
    _validate_causes(receipt)
    _validate_outcome(receipt)
    _validate_receipt_hashes(receipt, lane)


def _validate_receipt_identity(
    receipt: JsonObject,
    ledger: JsonObject,
    lane: str,
) -> None:
    if set(receipt) != ROOT_KEYS or receipt.get("schema_version") != 1:
        _fail("failure receipt has an open or unknown root")
    if receipt.get("attempt_id") != ledger.get("attempt_id"):
        _fail("failure receipt belongs to another attempt")
    foundation = receipt.get("sha")
    if foundation != ledger.get("foundation_sha") or not _matches(SHA40, foundation):
        _fail("failure receipt foundation SHA is invalid")
    if receipt.get("lane") != lane:
        _fail("failure receipt lane differs from its fixed filename")
    if receipt.get("stage") not in STAGES[lane]:
        _fail("failure receipt stage is invalid for its lane")


def _validate_receipt_timing(receipt: JsonObject) -> None:
    started = receipt.get("started_at_utc")
    ended = receipt.get("ended_at_utc")
    if not _matches(TIMESTAMP, started) or not _matches(TIMESTAMP, ended):
        _fail("failure receipt timestamp is not canonical UTC")
    if cast("str", started) > cast("str", ended):
        _fail("failure receipt ends before it starts")


def _validate_receipt_hashes(receipt: JsonObject, lane: str) -> None:
    if receipt.get("cleanup_verified") is not True:
        _fail("failure receipt lacks cleanup proof")
    for key in (
        "output_sha256",
        "lineage_validation_sha256",
        "journal_sha256",
        "control_tree_sha256",
    ):
        _nullable_sha256(receipt.get(key), key)
    if lane in {"F1", "F2", "F3"} and receipt.get("journal_sha256") is None:
        _fail("lane failure receipt lacks its sealed journal hash")
    if (
        receipt.get("stage") == "ORCH-input-freeze"
        and receipt.get("lineage_validation_sha256") is None
    ):
        _fail("input-freeze receipt lacks lineage validation")


def _validate_causes(receipt: JsonObject) -> None:
    value = receipt.get("observed_causes")
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        _fail("failure receipt causes are not a nonempty string array")
    causes = cast("list[str]", value)
    if any(item not in CAUSES for item in causes):
        _fail("failure receipt has an unknown cause")
    expected = sorted(set(causes), key=CAUSES.index)
    if causes != expected:
        _fail("failure receipt causes are duplicated or out of precedence order")
    cause = causes[0]
    if receipt.get("cause_code") != cause:
        _fail("failure receipt cause code is not its highest-precedence cause")
    if receipt.get("failure_class") != CAUSE_CLASS[cause]:
        _fail("failure receipt cause and class do not map")


def _validate_outcome(receipt: JsonObject) -> None:
    exit_code = receipt.get("exit_code")
    signal = receipt.get("signal")
    timed_out = receipt.get("timed_out")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        _fail("failure receipt exit code is invalid")
    if signal is not None and (
        not isinstance(signal, int) or isinstance(signal, bool) or signal <= 0
    ):
        _fail("failure receipt signal is invalid")
    if exit_code is not None and signal is not None:
        _fail("failure receipt cannot carry both exit code and signal")
    if not isinstance(timed_out, bool):
        _fail("failure receipt timeout marker is not boolean")
    causes = cast("list[str]", receipt["observed_causes"])
    if timed_out != ("deadline-exceeded" in causes):
        _fail("failure receipt timeout marker differs from its causes")
    if receipt.get("cause_code") == "boot-changed" and any(
        value is not None for value in (exit_code, signal, receipt.get("output_sha256"))
    ):
        _fail("boot-disappearance receipt invented a wait or output result")


def _nullable_sha256(value: JsonValue, context: str) -> None:
    if value is not None and not _matches(SHA256, value):
        _fail(f"failure receipt {context} is not a nullable SHA-256")


def _absolute_text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        _fail(f"{context} is not an absolute path")
    return value


def _matches(pattern: re.Pattern[str], value: JsonValue) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _fail(message: str) -> Never:
    raise IsolationError(message)
