"""Authenticate immutable primary receipts and their complete source set."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Final, NamedTuple, Never

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
from ops.testing.publish_todo_receipt import validate_todo_receipt
from ops.testing.validate_review_fix_receipt import (
    GitReader,
    validate_review_fix_receipt,
)

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
PRIMARY_KEYS: Final = {
    "todo",
    "attempt_id",
    "bundle_path",
    "relative_path",
    "sha256",
    "primary_commit_sha",
}
FIX_KEYS: Final = {
    "sequence",
    "attempt_id",
    "bundle_path",
    "relative_path",
    "sha256",
    "fix_commit_sha",
}
IMMUTABLE_HASHES: Final = {
    "ops/testing/publish_todo_receipt.py": (
        "8212738407c93ff8aed3fbf33dd2353e9e7c98a6ec3a034a947b60397caf26b9"
    ),
    "ops/testing/todo_receipt_validation.py": (
        "23b4b710711239a47b18b3bdf0411750ba6e8e1dd02b6b2b7e75733065a03e81"
    ),
    "ops/testing/todo-receipt.schema.json": (
        "5bba60602726e83f1acb42a0ae69e30e32412d862c20f3b5b0807851b4af5735"
    ),
}


class ReceiptSourceLocations(NamedTuple):
    """Authenticated worktree and current-attempt source locations."""

    worktree: Path
    current_attempt_root: Path
    current_attempt_id: str


def validate_primary_receipt_file(path: Path, commit: str) -> bytes:
    """Validate immutable identity, canonical bytes, and committed TDD evidence."""
    _immutable_validator_check()
    try:
        regular_identity(path, mode=MODE_IMMUTABLE)
    except IsolationError as error:
        message = "primary receipt must be a mode 0400 regular file"
        raise IsolationError(message) from error
    receipt, raw = load_json(path)
    validated = validate_todo_receipt(receipt, commit)
    if raw != validated or raw != canonical_bytes(receipt):
        _fail("primary receipt bytes are not canonical")
    todo = receipt.get("todo")
    expected = f"task-{todo}-clinic-os-phase-1a-staff-scheduling.json"
    if path.name != expected:
        _fail("primary receipt path does not match its todo number")
    return raw


def validate_receipt_sources(
    locations: ReceiptSourceLocations,
    primary_sources: JsonValue,
    fix_sources: JsonValue,
    candidate_sha: str,
    git: GitReader,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve exactly twenty primaries and one globally contiguous fix chain."""
    primaries = _objects(primary_sources, PRIMARY_KEYS, "primary")
    fixes = _objects(fix_sources, FIX_KEYS, "fix")
    todos = [_integer(item.get("todo"), "primary todo") for item in primaries]
    if todos != list(range(1, 21)):
        _fail("primary receipt source set is not exactly todos 1 through 20")
    primary_commits: list[str] = []
    primary_hashes: list[str] = []
    for source in primaries:
        todo = _integer(source.get("todo"), "primary todo")
        commit = _sha40(source.get("primary_commit_sha"), "primary commit")
        expected = f"todo-evidence/task-{todo}-clinic-os-phase-1a-staff-scheduling.json"
        path = _source_path(
            locations.worktree,
            locations.current_attempt_root,
            locations.current_attempt_id,
            source,
            expected,
        )
        raw = validate_primary_receipt_file(path, commit)
        _source_hash(source, raw)
        primary_commits.append(commit)
        primary_hashes.append(raw_sha256(raw))
    sequences = [_integer(item.get("sequence"), "fix sequence") for item in fixes]
    if sequences != list(range(1, len(fixes) + 1)):
        _fail("supplemental fix sequence is not globally contiguous")
    fix_commits: list[str] = []
    fix_hashes: list[str] = []
    for source in fixes:
        sequence = _integer(source.get("sequence"), "fix sequence")
        commit = _sha40(source.get("fix_commit_sha"), "fix commit")
        expected = f"todo-evidence/review-fix-{sequence}.json"
        path = _source_path(
            locations.worktree,
            locations.current_attempt_root,
            locations.current_attempt_id,
            source,
            expected,
        )
        regular_identity(path, mode=MODE_IMMUTABLE)
        receipt, raw = load_json(path)
        if validate_review_fix_receipt(receipt, commit, git) != raw:
            _fail("supplemental fix receipt bytes are not canonical")
        _source_hash(source, raw)
        fix_commits.append(commit)
        fix_hashes.append(raw_sha256(raw))
    _validate_commit_coverage(primary_commits[-1], candidate_sha, fix_commits, git)
    return tuple(primary_hashes), tuple(fix_hashes)


def _validate_commit_coverage(
    last_primary: str,
    candidate: str,
    fixes: list[str],
    git: GitReader,
) -> None:
    candidate = _sha40(candidate, "candidate SHA")
    raw = git("rev-list", "--reverse", f"{last_primary}..{candidate}")
    commits = raw.decode().splitlines()
    if commits != fixes:
        _fail("post-primary commits are not covered one-for-one by fix receipts")


def _source_path(
    worktree: Path,
    current_root: Path,
    current_attempt_id: str,
    source: JsonObject,
    expected_relative: str,
) -> Path:
    attempt = source.get("attempt_id")
    bundle = source.get("bundle_path")
    relative = source.get("relative_path")
    if not isinstance(attempt, str) or relative != expected_relative:
        _fail("receipt source logical locator is invalid")
    relative_path = PurePosixPath(expected_relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        _fail("receipt source path escapes its attempt root")
    if attempt == current_attempt_id:
        if bundle is not None:
            _fail("current-attempt receipt source has a bundle path")
        return current_root / relative_path
    expected_bundle = f".omo/evidence/clinic-os-phase1a-rejected/{attempt}/"
    if not isinstance(bundle, str) or not bundle.startswith(expected_bundle):
        _fail("archived receipt source has an invalid bundle path")
    bundle_path = PurePosixPath(bundle)
    if bundle_path.is_absolute() or ".." in bundle_path.parts:
        _fail("archived bundle path escapes the worktree")
    return worktree / bundle_path / "attempt" / relative_path


def _objects(value: JsonValue, keys: set[str], context: str) -> list[JsonObject]:
    if not isinstance(value, list):
        _fail(f"{context} receipt sources must be an array")
    result: list[JsonObject] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != keys:
            _fail(f"{context} receipt source has an open root")
        result.append(item)
    return result


def _source_hash(source: JsonObject, raw: bytes) -> None:
    digest = source.get("sha256")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        _fail("receipt source SHA-256 is invalid")
    if raw_sha256(raw) != digest:
        _fail("receipt source hash differs from immutable bytes")


def _immutable_validator_check() -> None:
    for relative, expected in IMMUTABLE_HASHES.items():
        if raw_sha256((PROJECT_ROOT / relative).read_bytes()) != expected:
            _fail("Todo 1 receipt validator or primary schema drifted")


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{context} is invalid")
    return value


def _sha40(value: JsonValue, context: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        _fail(f"{context} must be lowercase 40-hex")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
