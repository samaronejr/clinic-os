from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = PROJECT_ROOT / "ops/testing/review-fix-receipt.schema.json"
EXPECTED_KEYS = {
    "acceptance",
    "change_class",
    "changed_paths",
    "cleanup",
    "fix_commit_sha",
    "owning_todo",
    "parent_sha",
    "rejected_sha",
    "resources",
    "review_lane",
    "schema_version",
    "sequence",
    "tdd_entries",
    "tests_after_entries",
}
COMMIT = "a" * 40
PARENT = "b" * 40
REJECTED = "c" * 40
ZERO_SHA256 = "0" * 64
TIMESTAMP = "2026-08-18T12:00:00.000000Z"


def test_review_fix_schema_has_the_closed_plan_key_set() -> None:
    # Given: the supplemental receipt schema used after a source-fix rejection.
    assert SCHEMA.is_file()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

    # When / Then: its root is closed over exactly the plan-authorized keys.
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == EXPECTED_KEYS
    assert set(schema["properties"]) == EXPECTED_KEYS


def test_docs_fix_validator_binds_commit_paths_and_trailers() -> None:
    # Given: a docs-only fix and its exact one-parent Git evidence.
    module_path = PROJECT_ROOT / "ops/testing/validate_review_fix_receipt.py"
    assert module_path.is_file()
    specification = importlib.util.spec_from_file_location("review_fix", module_path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    receipt: JsonObject = {
        "acceptance": [_command(["git", "diff", "--check"]), _command(["link-check"])],
        "change_class": "docs-only",
        "changed_paths": ["docs/guide.md"],
        "cleanup": _cleanup(),
        "fix_commit_sha": COMMIT,
        "owning_todo": 19,
        "parent_sha": PARENT,
        "rejected_sha": REJECTED,
        "resources": [],
        "review_lane": "F1",
        "schema_version": 1,
        "sequence": 1,
        "tdd_entries": [],
        "tests_after_entries": [],
    }

    def git(*arguments: str) -> bytes:
        responses: dict[tuple[str, ...], bytes] = {
            ("rev-parse", f"{COMMIT}^{{commit}}"): f"{COMMIT}\n".encode(),
            ("rev-parse", f"{COMMIT}^"): f"{PARENT}\n".encode(),
            (
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                COMMIT,
            ): b"docs/guide.md\0",
            ("show", "-s", "--format=%B", COMMIT): (
                f"docs: repair review finding\n\nOwning-todo: 19\n"
                f"Review-lane: F1\nRejected-SHA: {REJECTED}\n"
            ).encode(),
        }
        return responses[arguments]

    # When / Then: validation returns canonical bytes only for that exact commit.
    validated = module.validate_review_fix_receipt(receipt, COMMIT, git)
    assert json.loads(validated) == receipt


def test_primary_validator_rejects_a_mutable_archived_receipt(tmp_path: Path) -> None:
    # Given: receipt bytes that remain mutable instead of sealed mode 0400.
    module_path = PROJECT_ROOT / "ops/testing/validate_tdd_receipt.py"
    assert module_path.is_file()
    specification = importlib.util.spec_from_file_location(
        "primary_receipt", module_path
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    receipt_path = tmp_path / "task-1-clinic-os-phase-1a-staff-scheduling.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    receipt_path.chmod(0o600)

    # When / Then: mode authentication fails before receipt content is trusted.
    with pytest.raises(Exception, match="mode 0400"):
        module.validate_primary_receipt_file(receipt_path, COMMIT)


def _command(argv: list[str]) -> JsonObject:
    json_argv: list[JsonValue] = list(argv)
    return {
        "argv": json_argv,
        "ended_at_utc": TIMESTAMP,
        "exit_code": 0,
        "started_at_utc": TIMESTAMP,
        "stderr_sha256": ZERO_SHA256,
        "stdout_sha256": ZERO_SHA256,
    }


def _cleanup() -> JsonObject:
    return {
        "claim_ids_absent": True,
        "containers_absent": True,
        "listeners_absent": True,
        "networks_absent": True,
        "paths_absent": True,
        "process_members_absent": True,
        "shared_evidence_manifest_sha256": ZERO_SHA256,
        "verified_at_utc": TIMESTAMP,
        "volumes_absent": True,
    }
