"""Validate and no-replace publish one primary Phase 1A todo receipt."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Final, Never

from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    raw_sha256,
)


def _values(raw: str, separator: str = " ") -> frozenset[str]:
    return frozenset(raw.split(separator))


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
MODULE_PATH: Final = PurePosixPath("ops/testing/publish_todo_receipt.py")
VALIDATION_MODULE_PATH: Final = PurePosixPath("ops/testing/todo_receipt_validation.py")
GIT: Final = shutil.which("git")
RECEIPT_KEYS: Final = _values(
    "schema_version todo foundation_sha parent_sha primary_commit_sha validator_sha256 "
    "tdd_entries tests_after_entries acceptance resources cleanup"
)
TODO_COUNT: Final = 20


def _fail(message: str) -> Never:
    raise IsolationError(message)


def validate_todo_receipt(receipt: JsonObject, commit: str) -> bytes:
    """Validate closed schema, Git lineage, TDD chronology, and redaction."""
    commit = _hex(commit, 40, "primary commit")
    _exact(receipt, RECEIPT_KEYS, "receipt")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("primary_commit_sha") != commit
    ):
        _fail("receipt version or primary commit does not match")
    todo = receipt["todo"]
    if isinstance(todo, bool) or not isinstance(todo, int):
        _fail("todo must be an integer")
    if not 1 <= todo <= TODO_COUNT:
        _fail("todo number is outside Phase 1A")
    foundation = _hex(receipt["foundation_sha"], 40, "foundation SHA")
    parent = _hex(receipt["parent_sha"], 40, "parent SHA")
    if _git("rev-parse", f"{commit}^{{commit}}").decode().strip() != commit:
        _fail("primary commit does not resolve exactly")
    if _git("rev-parse", f"{commit}^").decode().strip() != parent:
        _fail("receipt parent is not the primary commit parent")
    _git("merge-base", "--is-ancestor", foundation, commit)
    module = _git("show", f"{commit}:{MODULE_PATH.as_posix()}")
    validation_module = _git(
        "show",
        f"{commit}:{VALIDATION_MODULE_PATH.as_posix()}",
    )
    current_module = Path(__file__).read_bytes()
    if module != current_module or receipt.get("validator_sha256") != raw_sha256(
        module
    ):
        _fail("receipt validator is not the clean committed module")
    if validation_module != _validation_module_bytes():
        _fail("receipt validator dependency is not the clean committed module")
    from ops.testing.todo_receipt_validation import (  # noqa: PLC0415
        validate_committed_receipt_evidence,
    )

    validate_committed_receipt_evidence(receipt, commit, _git)
    return canonical_bytes(receipt)


def _validation_module_bytes() -> bytes:
    return (PROJECT_ROOT / VALIDATION_MODULE_PATH).read_bytes()


def _git(*arguments: str) -> bytes:
    if GIT is None:
        _fail("git executable is unavailable")
    result = subprocess.run(  # noqa: S603 - resolved Git binary and tuple argv.
        (GIT, "-C", PROJECT_ROOT, *arguments),
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        _fail("git rejected todo-receipt validation")
    return result.stdout


def _exact(value: JsonObject, keys: frozenset[str], context: str) -> None:
    if set(value) != keys:
        _fail(f"{context} has the wrong closed key set")


def _hex(value: JsonValue, length: int, context: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None
    ):
        _fail(f"{context} must be lowercase {length}-hex")
    return value


if __name__ == "__main__":
    from ops.testing.todo_receipt_cli import run_cli

    raise SystemExit(run_cli(validate_todo_receipt))
