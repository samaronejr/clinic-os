"""Validate one supplemental review-fix receipt against its exact commit."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Final, Never, Protocol

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    raw_sha256,
)
from ops.testing.todo_receipt_validation import validate_committed_receipt_evidence


class GitReader(Protocol):
    """Read exact Git object bytes for receipt validation."""

    def __call__(self, *arguments: str) -> bytes:
        """Read bytes for one closed Git argument tuple."""
        ...


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SHA40: Final = re.compile(r"^[0-9a-f]{40}$")
ROOT_KEYS: Final = frozenset(
    [
        "schema_version",
        "sequence",
        "change_class",
        "owning_todo",
        "review_lane",
        "rejected_sha",
        "parent_sha",
        "fix_commit_sha",
        "changed_paths",
        "tdd_entries",
        "tests_after_entries",
        "acceptance",
        "resources",
        "cleanup",
    ]
)
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
CLASSES: Final = {"behavior", "tests", "visual-polish", "docs-only"}
LANES: Final = {"F1", "F2", "F3", "F4", "ORCH", "USER"}
LAST_TODO: Final = 20


def validate_review_fix_receipt(
    receipt: JsonObject,
    commit: str,
    git: GitReader | None = None,
    *,
    originating_orch_source_fix: bool = False,
) -> bytes:
    """Bind closed supplemental evidence to one parent, diff, and trailer set."""
    _immutable_validator_check()
    if set(receipt) != ROOT_KEYS or receipt.get("schema_version") != 1:
        _fail("review-fix receipt has an open root or wrong version")
    reader = git if git is not None else _git
    commit = _sha(commit, "fix commit")
    if receipt.get("fix_commit_sha") != commit:
        _fail("review-fix receipt commit does not match")
    parent = _sha(receipt.get("parent_sha"), "fix parent")
    rejected = _sha(receipt.get("rejected_sha"), "rejected SHA")
    if reader("rev-parse", f"{commit}^{{commit}}").decode().strip() != commit:
        _fail("fix commit does not resolve exactly")
    if reader("rev-parse", f"{commit}^").decode().strip() != parent:
        _fail("fix commit does not have the receipt parent")
    sequence = receipt.get("sequence")
    todo = receipt.get("owning_todo")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        _fail("review-fix sequence is invalid")
    if (
        isinstance(todo, bool)
        or not isinstance(todo, int)
        or not 1 <= todo <= LAST_TODO
    ):
        _fail("review-fix owning todo is invalid")
    change_class, lane = receipt.get("change_class"), receipt.get("review_lane")
    if change_class not in CLASSES or lane not in LANES:
        _fail("review-fix class or lane is invalid")
    if lane == "ORCH" and not originating_orch_source_fix:
        _fail("ORCH review-fix lacks a source-fix receipt")
    paths = _paths(receipt.get("changed_paths"))
    changed = _nul_paths(
        reader("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit)
    )
    if paths != changed:
        _fail("review-fix changed paths differ from the commit")
    message = reader("show", "-s", "--format=%B", commit).decode()
    _trailer(message, "Owning-todo", str(todo))
    _trailer(message, "Review-lane", str(lane))
    _trailer(message, "Rejected-SHA", rejected)
    _validate_class(receipt, str(change_class), paths, commit, reader)
    return canonical_bytes(receipt)


def _validate_class(
    receipt: JsonObject,
    change_class: str,
    paths: list[str],
    commit: str,
    git: GitReader,
) -> None:
    tdd = receipt.get("tdd_entries")
    tests_after = receipt.get("tests_after_entries")
    if not isinstance(tdd, list) or not isinstance(tests_after, list):
        _fail("review-fix test evidence is invalid")
    if change_class in {"behavior", "tests"} and not tdd:
        _fail("behavior and test fixes require red-green evidence")
    if change_class == "visual-polish" and not tests_after:
        _fail("visual fixes require tests-after evidence")
    if change_class == "docs-only":
        if (
            tdd
            or tests_after
            or any(
                not (path.startswith("docs/") or path.endswith(".md")) for path in paths
            )
        ):
            _fail("docs-only fix contains non-document behavior")
        _validate_docs_acceptance(receipt.get("acceptance"))
        _validate_common(receipt)
        return
    proxy: JsonObject = {
        "acceptance": receipt["acceptance"],
        "cleanup": receipt["cleanup"],
        "resources": receipt["resources"],
        "tdd_entries": receipt["tdd_entries"],
        "tests_after_entries": receipt["tests_after_entries"],
    }
    validate_committed_receipt_evidence(proxy, commit, git)


def _validate_common(receipt: JsonObject) -> None:
    acceptance = receipt.get("acceptance")
    resources = receipt.get("resources")
    cleanup = receipt.get("cleanup")
    if not isinstance(acceptance, list) or not all(
        isinstance(item, dict) and item.get("exit_code") == 0 for item in acceptance
    ):
        _fail("review-fix acceptance evidence is invalid")
    if resources != []:
        _fail("docs-only fix cannot claim resources")
    if not isinstance(cleanup, dict):
        _fail("review-fix cleanup evidence is invalid")
    absence = (
        "claim_ids_absent",
        "paths_absent",
        "containers_absent",
        "volumes_absent",
        "networks_absent",
        "listeners_absent",
        "process_members_absent",
    )
    if any(cleanup.get(key) is not True for key in absence):
        _fail("review-fix cleanup absence proof is incomplete")


def _validate_docs_acceptance(value: JsonValue) -> None:
    if not isinstance(value, list):
        _fail("docs-only acceptance is invalid")
    commands: list[list[str]] = []
    for item in value:
        if not isinstance(item, dict):
            _fail("docs-only acceptance is invalid")
        argv = item.get("argv")
        if not isinstance(argv, list) or not all(
            isinstance(part, str) for part in argv
        ):
            _fail("docs-only acceptance argv is invalid")
        commands.append([part for part in argv if isinstance(part, str)])
    diff_check = ["git", "diff", "--check"]
    if diff_check not in commands or not any(
        any("link" in part.casefold() for part in command) for command in commands
    ):
        _fail("docs-only fix lacks link and diff acceptance")


def _paths(value: JsonValue) -> list[str]:
    if not isinstance(value, list) or not value:
        _fail("review-fix paths are invalid")
    paths: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            _fail("review-fix paths are invalid")
        path = PurePosixPath(item)
        if path.is_absolute() or ".." in path.parts:
            _fail("review-fix path escapes the repository")
        paths.append(item)
    if paths != sorted(set(paths)):
        _fail("review-fix paths are not sorted and unique")
    return paths


def _nul_paths(raw: bytes) -> list[str]:
    paths = raw.rstrip(b"\0").decode().split("\0") if raw else []
    if paths != sorted(set(paths)):
        _fail("fix commit paths are not sorted and unique")
    return paths


def _trailer(message: str, name: str, expected: str) -> None:
    values = [
        line.removeprefix(f"{name}: ")
        for line in message.splitlines()
        if line.startswith(f"{name}: ")
    ]
    if values != [expected]:
        _fail(f"review-fix {name} trailer is missing or wrong")


def _immutable_validator_check() -> None:
    for relative, expected in IMMUTABLE_HASHES.items():
        if raw_sha256((PROJECT_ROOT / relative).read_bytes()) != expected:
            _fail("Todo 1 receipt validator or primary schema drifted")


def _sha(value: JsonValue, context: str) -> str:
    if not isinstance(value, str) or SHA40.fullmatch(value) is None:
        _fail(f"{context} must be lowercase 40-hex")
    return value


def _git(*arguments: str) -> bytes:
    executable = shutil.which("git")
    if executable is None:
        _fail("git executable is unavailable")
    read_fd, write_fd = os.pipe()
    pid = os.posix_spawn(
        executable,
        (executable, "-C", str(PROJECT_ROOT), *arguments),
        os.environ,
        file_actions=(
            (os.POSIX_SPAWN_CLOSE, read_fd),
            (os.POSIX_SPAWN_DUP2, write_fd, 1),
            (os.POSIX_SPAWN_CLOSE, write_fd),
        ),
    )
    os.close(write_fd)
    chunks: list[bytes] = []
    while chunk := os.read(read_fd, 65_536):
        chunks.append(chunk)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        _fail("git rejected review-fix validation")
    return b"".join(chunks)


def _fail(message: str) -> Never:
    raise IsolationError(message)
