from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from ops.testing import publish_todo_receipt
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Callable

COMMIT: Final = "a" * 40
FOUNDATION: Final = "b" * 40
PARENT: Final = "c" * 40
TEST_PATH: Final = "tests/test_receipt_subject.py"
TEST_SOURCE: Final = b"def test_subject():\n    assert True\n"
TIMESTAMP: Final = "2026-07-16T12:00:00.000000Z"
ZERO_SHA256: Final = "0" * 64
CLAIM_ID: Final = "12345678-1234-4123-8123-123456789abc"
HTTP_NODEID: Final = "tests/test_receipt_subject.py::test_subject"
VALIDATOR_PATH: Final = Path(publish_todo_receipt.__file__)


def _source_sha256() -> str:
    digest = hashlib.sha256()
    digest.update(TEST_PATH.encode())
    digest.update(b"\0")
    digest.update(len(TEST_SOURCE).to_bytes(8, "big"))
    digest.update(TEST_SOURCE)
    return digest.hexdigest()


def _run(exit_code: int, classification: str) -> JsonObject:
    observed: list[JsonValue] = [HTTP_NODEID] if exit_code else []
    return {
        "argv": ["uv", "run", "pytest", TEST_PATH, "-q"],
        "classification": classification,
        "ended_at_utc": TIMESTAMP,
        "exit_code": exit_code,
        "expected_failed_nodeids": [HTTP_NODEID],
        "observed_failed_nodeids": observed,
        "started_at_utc": TIMESTAMP,
        "stderr_sha256": ZERO_SHA256,
        "stdout_sha256": ZERO_SHA256,
    }


def _command() -> JsonObject:
    return {
        "argv": ["uv", "run", "pytest", TEST_PATH, "-q"],
        "ended_at_utc": TIMESTAMP,
        "exit_code": 0,
        "started_at_utc": TIMESTAMP,
        "stderr_sha256": ZERO_SHA256,
        "stdout_sha256": ZERO_SHA256,
    }


def _receipt() -> JsonObject:
    test_source_sha256 = _source_sha256()
    return {
        "acceptance": [_command()],
        "cleanup": {
            "claim_ids_absent": True,
            "containers_absent": True,
            "listeners_absent": True,
            "networks_absent": True,
            "paths_absent": True,
            "process_members_absent": True,
            "shared_evidence_manifest_sha256": ZERO_SHA256,
            "verified_at_utc": TIMESTAMP,
            "volumes_absent": True,
        },
        "foundation_sha": FOUNDATION,
        "parent_sha": PARENT,
        "primary_commit_sha": COMMIT,
        "resources": [
            {
                "activated_at_utc": TIMESTAMP,
                "claim_id": CLAIM_ID,
                "kind": "filesystem",
                "released_at_utc": TIMESTAMP,
            }
        ],
        "schema_version": 1,
        "tdd_entries": [
            {
                "green": _run(0, "pass"),
                "red": _run(1, "expected-assertion-failure"),
                "test_paths": [TEST_PATH],
                "test_source_sha256": test_source_sha256,
            }
        ],
        "tests_after_entries": [
            {
                **_command(),
                "classification": "visual-polish-no-behavior",
                "http_contract_nodeids": [HTTP_NODEID],
                "test_paths": [TEST_PATH],
                "test_source_sha256": test_source_sha256,
            }
        ],
        "todo": 1,
        "validator_sha256": hashlib.sha256(VALIDATOR_PATH.read_bytes()).hexdigest(),
    }


@pytest.fixture
def fake_git(monkeypatch: pytest.MonkeyPatch) -> Callable[[JsonObject, str], bytes]:
    module_source = VALIDATOR_PATH.read_bytes()
    validation_source = (
        VALIDATOR_PATH.parent / "todo_receipt_validation.py"
    ).read_bytes()

    def run(receipt: JsonObject, commit: str = COMMIT) -> bytes:
        def git(*arguments: str) -> bytes:
            if arguments == ("rev-parse", f"{commit}^{{commit}}"):
                return f"{commit}\n".encode()
            if arguments == ("rev-parse", f"{commit}^"):
                return f"{PARENT}\n".encode()
            if arguments[:3] == ("merge-base", "--is-ancestor", FOUNDATION):
                return b""
            module_sources = {
                f"{commit}:ops/testing/publish_todo_receipt.py": module_source,
                f"{commit}:ops/testing/todo_receipt_validation.py": validation_source,
            }
            if arguments[:1] == ("show",) and arguments[1] in module_sources:
                return module_sources[arguments[1]]
            if arguments[:4] == (
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
            ):
                return f"{TEST_PATH}\0".encode()
            if arguments == ("show", f"{commit}:{TEST_PATH}"):
                return TEST_SOURCE
            raise AssertionError(arguments)

        monkeypatch.setattr(publish_todo_receipt, "_git", git)
        return publish_todo_receipt.validate_todo_receipt(receipt, commit)

    return run


def test_receipt_rejects_a_nonhex_primary_commit(
    fake_git: Callable[[JsonObject, str], bytes],
) -> None:
    # Given: a receipt whose primary commit matches a nonhex CLI argument.
    receipt = _receipt()
    invalid_commit = "x" * 40
    receipt["primary_commit_sha"] = invalid_commit

    # When: the public receipt validator parses the trust-boundary value.
    with pytest.raises(IsolationError, match="primary commit"):
        fake_git(receipt, invalid_commit)

    # Then: the invalid Git object name is never accepted as evidence identity.


def test_receipt_rejects_an_uncommitted_validator_dependency_before_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the committed validator dependency differs from the local dependency.
    receipt = _receipt()

    def git(*arguments: str) -> bytes:
        if arguments == ("rev-parse", f"{COMMIT}^{{commit}}"):
            return f"{COMMIT}\n".encode()
        if arguments == ("rev-parse", f"{COMMIT}^"):
            return f"{PARENT}\n".encode()
        if arguments[:3] == ("merge-base", "--is-ancestor", FOUNDATION):
            return b""
        if arguments == ("show", f"{COMMIT}:ops/testing/publish_todo_receipt.py"):
            return VALIDATOR_PATH.read_bytes()
        if arguments == (
            "show",
            f"{COMMIT}:ops/testing/todo_receipt_validation.py",
        ):
            return b"committed dependency differs\n"
        raise AssertionError(arguments)

    monkeypatch.setattr(publish_todo_receipt, "_git", git)

    # When: validation reaches the dependency trust boundary.
    with pytest.raises(IsolationError, match="dependency"):
        publish_todo_receipt.validate_todo_receipt(receipt, COMMIT)

    # Then: no receipt evidence is consumed through uncommitted validation code.


def test_receipt_rejects_invalid_tests_after_command_metadata(
    fake_git: Callable[[JsonObject, str], bytes],
) -> None:
    # Given: tests-after evidence has a non-string command argument.
    receipt = _receipt()
    tests_after = receipt["tests_after_entries"]
    assert isinstance(tests_after, list)
    assert isinstance(tests_after[0], dict)
    tests_after[0]["argv"] = ["uv", 1]

    # When: the public receipt validator checks behavior-neutral evidence.
    with pytest.raises(IsolationError, match="argv"):
        fake_git(receipt, COMMIT)

    # Then: malformed tests-after command identity cannot be published.


def test_receipt_accepts_the_exact_normative_shape(
    fake_git: Callable[[JsonObject, str], bytes],
) -> None:
    # Given: a receipt with line-624 green, HTTP, lifecycle, and cleanup evidence.
    receipt = _receipt()

    # When: the public validator checks the complete closed receipt.
    validated = fake_git(receipt, COMMIT)

    # Then: it returns the exact canonical bytes and no alternate shape.
    assert validated == canonical_bytes(receipt)


def test_receipt_rejects_tests_after_without_green_http_contract_nodeids(
    fake_git: Callable[[JsonObject, str], bytes],
) -> None:
    # Given: visual-polish evidence whose HTTP node is not in the green TDD set.
    receipt = _receipt()
    tests_after = receipt["tests_after_entries"]
    assert isinstance(tests_after, list)
    assert isinstance(tests_after[0], dict)
    tests_after[0]["http_contract_nodeids"] = [
        "tests/test_receipt_subject.py::test_unproven"
    ]

    # When: the public validator binds tests-after to earlier green contracts.
    with pytest.raises(IsolationError, match="HTTP contract"):
        fake_git(receipt, COMMIT)

    # Then: visual-only evidence cannot invent a later behavioral contract.


def test_receipt_rejects_non_uuid_resource_claim_ids(
    fake_git: Callable[[JsonObject, str], bytes],
) -> None:
    # Given: resource lifecycle evidence carries a non-UUID claim value.
    receipt = _receipt()
    resources = receipt["resources"]
    assert isinstance(resources, list)
    assert isinstance(resources[0], dict)
    resources[0]["claim_id"] = "not-a-claim"

    # When: the public receipt validator checks resource evidence identity.
    with pytest.raises(IsolationError, match="claim ID"):
        fake_git(receipt, COMMIT)

    # Then: an ambiguous resource identity cannot enter immutable evidence.


def test_receipt_rejects_incomplete_cleanup_absence_proof(
    fake_git: Callable[[JsonObject, str], bytes],
) -> None:
    # Given: cleanup records one resource class as still present.
    receipt = _receipt()
    cleanup = receipt["cleanup"]
    assert isinstance(cleanup, dict)
    cleanup["listeners_absent"] = False

    # When: the public validator checks the exact absence proof.
    with pytest.raises(IsolationError, match="absence"):
        fake_git(receipt, COMMIT)

    # Then: a receipt cannot publish while any task resource remains.
