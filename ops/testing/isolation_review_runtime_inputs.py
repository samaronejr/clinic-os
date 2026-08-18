"""Authenticate frozen bindings consumed by durable review controllers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Never

from ops.testing.isolation_claim_records import claim_objects
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
    load_json,
    raw_sha256,
    regular_identity,
)
from ops.testing.isolation_ledger_store import locked_open_ledger
from ops.testing.isolation_terminal_publisher_authorizations import (
    validate_terminal_publisher_claim,
)
from ops.testing.isolation_tool_identity import (
    authenticate_launcher,
    authenticate_static_amd64_launcher,
)

PRIMARY_RECEIPT_COUNT = 20


@dataclass(frozen=True, slots=True)
class ReviewRuntimeInputs:
    """Closed paths and identities authorized by final inputs and the ledger."""

    attempt_id: str
    inputs_sha256: str
    lineage_validation_sha256: str
    ledger_path: Path
    attempt_root: Path
    controller_root: Path
    terminal_root: Path
    worktree: Path
    publisher_claim_id: str
    codex_path: Path
    codex_sha256: str
    uv_path: Path
    uv_sha256: str
    approved_plan: Path
    approved_sidecar: Path
    receipt_paths: tuple[Path, ...]


def load_review_runtime_inputs(inputs_path: Path, sha: str) -> ReviewRuntimeInputs:
    """Load final inputs and revalidate their live ledger/publisher authority."""
    regular_identity(inputs_path, mode=MODE_IMMUTABLE)
    manifest, raw = load_json(inputs_path)
    if manifest.get("schema_version") != 1 or manifest.get("sha") != sha:
        _fail("review final-input identity differs from the requested SHA")
    runtime = _object(manifest.get("runtime_bindings"), "runtime bindings")
    review = _object(manifest.get("review_inputs"), "review inputs")
    ledger_path = _absolute(runtime.get("ledger_path"), "ledger path")
    attempt_root = _absolute(runtime.get("attempt_root"), "attempt root")
    controller_root = _absolute(
        runtime.get("review_controller_root"), "review controller root"
    )
    terminal_root = _absolute(runtime.get("terminal_root"), "terminal root")
    worktree = _absolute(runtime.get("worktree"), "worktree")
    if (
        controller_root != attempt_root / "review-lane-controllers"
        or terminal_root != attempt_root.parents[1] / "clinic-os-phase1a-final/terminal"
    ):
        _fail("review runtime paths differ from their fixed authorities")
    codex_path, codex_sha = _launcher(
        runtime.get("codex_launcher"), "Codex", static=True
    )
    uv_path, uv_sha = _launcher(runtime.get("uv_launcher"), "uv", static=False)
    publisher_claim_id = _text(
        manifest.get("terminal_publisher_claim_id"), "publisher claim ID"
    )
    attempt_id = _text(manifest.get("attempt_id"), "attempt ID")
    with locked_open_ledger(ledger_path) as session:
        ledger = session.ledger
        if (
            ledger.get("attempt_id") != attempt_id
            or ledger.get("attempt_root") != str(attempt_root)
            or ledger.get("worktree_realpath") != str(worktree.resolve())
        ):
            _fail("review ledger identity differs from frozen inputs")
        matches = [
            item
            for item in claim_objects(ledger["claims"])
            if item.get("claim_id") == publisher_claim_id
        ]
        if len(matches) != 1:
            _fail("review terminal publisher is missing or duplicated")
        _ = validate_terminal_publisher_claim(matches[0], terminal_root)
    receipts = _review_receipts(review.get("receipts"))
    return ReviewRuntimeInputs(
        attempt_id,
        raw_sha256(raw),
        _text(manifest.get("lineage_validation_sha256"), "lineage validation SHA-256"),
        ledger_path,
        attempt_root,
        controller_root,
        terminal_root,
        worktree,
        publisher_claim_id,
        codex_path,
        codex_sha,
        uv_path,
        uv_sha,
        _absolute(review.get("approved_plan"), "approved plan"),
        _absolute(review.get("approved_sidecar"), "approved sidecar"),
        receipts,
    )


def _launcher(value: JsonValue, context: str, *, static: bool) -> tuple[Path, str]:
    record = _object(value, f"{context} launcher")
    source = _absolute(record.get("source_path"), f"{context} source path")
    observed = (
        authenticate_static_amd64_launcher(source)
        if static
        else authenticate_launcher(source)
    )
    if observed != record:
        _fail(f"{context} launcher identity drifted")
    path = _absolute(record.get("resolved_path"), f"{context} resolved path")
    expected = _text(record.get("resolved_sha256"), f"{context} SHA-256")
    if raw_sha256(path.read_bytes()) != expected:
        _fail(f"{context} launcher bytes drifted")
    return path, expected


def _review_receipts(value: JsonValue) -> tuple[Path, ...]:
    records = _objects(value, "review receipts")
    paths: list[Path] = []
    for record in records:
        path = _absolute(record.get("path"), "review receipt path")
        if raw_sha256(path.read_bytes()) != record.get("sha256"):
            _fail("review receipt bytes drifted")
        paths.append(path)
    if len(paths) != PRIMARY_RECEIPT_COUNT or len(set(paths)) != len(paths):
        _fail("review receipt set is not exactly twenty unique paths")
    return tuple(paths)


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} is not an object")
    return value


def _objects(value: JsonValue, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} is not an object array")
    return [item for item in value if isinstance(item, dict)]


def _absolute(value: JsonValue, context: str) -> Path:
    text = _text(value, context)
    path = Path(text)
    if not path.is_absolute():
        _fail(f"{context} is not absolute")
    return path


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} is not text")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
