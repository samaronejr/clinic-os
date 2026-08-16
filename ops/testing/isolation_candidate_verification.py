"""Recheck clean Git and immutable Docker metadata for candidate binding."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Final, Never, cast

from ops.testing.isolation_common import IsolationError, JsonObject, JsonValue

COMMAND_TIMEOUT_SECONDS: Final = 30
MAX_OUTPUT_BYTES: Final = 16 * 1024 * 1024


def _fail(message: str) -> Never:
    raise IsolationError(message)


def verify_candidate_image(
    ledger: JsonObject,
    _claim: JsonObject,
    envelope: JsonObject,
) -> None:
    """Verify the staged source identity against fresh Git and image inspect data."""
    worktree = Path(_text(ledger.get("worktree_realpath"), "worktree realpath"))
    if worktree.resolve(strict=True) != worktree:
        _fail("candidate worktree is noncanonical")
    revision = _text(envelope.get("revision_sha"), "candidate revision")
    tree = _text(envelope.get("tree_sha"), "candidate tree")
    if _git(worktree, "rev-parse", "HEAD").strip() != revision:
        _fail("candidate revision is not current HEAD")
    if _git(worktree, "rev-parse", "HEAD^{tree}").strip() != tree:
        _fail("candidate tree is not current HEAD tree")
    if _git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
        _fail("candidate worktree is not clean")
    image_id = _text(envelope.get("image_id"), "candidate image ID")
    inspection = _image_inspect(image_id)
    if inspection.get("Id") != image_id or inspection.get("Architecture") != "amd64":
        _fail("candidate image identity or architecture drifted")
    config = _object(inspection.get("Config"), "candidate image config")
    labels = _string_mapping(config.get("Labels"), "candidate image labels")
    contract = _object(envelope.get("image_contract"), "candidate image contract")
    expected = _expected_labels(contract)
    if any(labels.get(name) != value for name, value in expected.items()):
        _fail("candidate image labels do not match its contract")


def _expected_labels(contract: JsonObject) -> dict[str, str]:
    kind = _text(contract.get("kind"), "candidate image kind")
    revision = _text(contract.get("revision_sha"), "candidate contract revision")
    tree = _text(contract.get("tree_sha"), "candidate contract tree")
    manifest = _text(
        contract.get("source_manifest_sha256"), "candidate source manifest"
    )
    count = contract.get("source_entry_count")
    if isinstance(count, bool) or not isinstance(count, int):
        _fail("candidate source entry count is invalid")
    labels = {
        "clinic.phase1a.image-kind": kind,
        "clinic.phase1a.tree": tree,
        "org.opencontainers.image.revision": revision,
    }
    if kind == "application":
        labels["clinic.phase1a.application-source-sha256"] = manifest
        labels["clinic.phase1a.application-source-entry-count"] = str(count)
    elif kind == "browser-runner":
        suites = contract.get("available_suite_ids")
        if not isinstance(suites, list) or not all(
            isinstance(item, str) for item in suites
        ):
            _fail("candidate available suite IDs are invalid")
        labels["clinic.phase1a.runner-source-sha256"] = manifest
        labels["clinic.phase1a.runner-source-entry-count"] = str(count)
        labels["clinic.phase1a.available-suite-ids"] = ",".join(
            cast("list[str]", suites)
        )
    else:
        _fail("candidate image kind is invalid")
    return labels


def _git(worktree: Path, *arguments: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        _fail("git executable is unavailable")
    return _run((str(Path(executable).resolve()), "-C", str(worktree), *arguments))


def _image_inspect(image_id: str) -> JsonObject:
    executable = shutil.which("docker")
    if executable is None:
        _fail("docker executable is unavailable")
    raw = _run(
        (
            str(Path(executable).resolve()),
            "image",
            "inspect",
            "--format",
            "{{json .}}",
            image_id,
        )
    )
    try:
        value: JsonValue = json.loads(raw)
    except json.JSONDecodeError as error:
        message = "candidate image inspect returned invalid JSON"
        raise IsolationError(message) from error
    return _object(value, "candidate image inspect")


def _run(command: tuple[str, ...]) -> str:
    result = subprocess.run(  # noqa: S603 - resolved executable and closed argv.
        command,
        check=False,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if len(result.stdout) > MAX_OUTPUT_BYTES or len(result.stderr) > MAX_OUTPUT_BYTES:
        _fail("candidate verification command output is too large")
    if result.returncode != 0:
        _fail("candidate verification command failed")
    try:
        return result.stdout.decode()
    except UnicodeDecodeError as error:
        message = "candidate verification output is not UTF-8"
        raise IsolationError(message) from error


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _string_mapping(value: JsonValue, context: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        _fail(f"{context} must be a string mapping")
    return cast("dict[str, str]", value)


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value
