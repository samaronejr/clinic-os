"""Own review-lane filesystem claims and their authenticated tool copies."""

from __future__ import annotations

import os
import pwd
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_claim_records import claim_objects
from ops.testing.isolation_claim_transitions import (
    activate_claim,
    release_claim,
    reserve_claim,
)
from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    regular_identity,
    write_no_replace,
)
from ops.testing.isolation_review_lane_controller import ToolClosure

if TYPE_CHECKING:
    from ops.testing.isolation_review_runtime_inputs import ReviewRuntimeInputs


@dataclass(frozen=True, slots=True)
class ReviewFilesystems:
    """Active review/private roots bound to preallocated journal claim IDs."""

    review_claim_id: str
    private_claim_id: str | None
    review_root: Path
    private_root: Path | None
    closure: ToolClosure


def reserve_and_activate_review_filesystems(
    runtime: ReviewRuntimeInputs,
    lane: str,
    review_claim_id: str,
    private_claim_id: str | None,
    inputs_path: Path,
) -> ReviewFilesystems:
    """Reserve, materialize, observe, and activate one lane's filesystem roots."""
    reserved: list[str] = []
    review_root = runtime.attempt_root / "claims" / review_claim_id
    private_root = (
        runtime.attempt_root / "claims" / private_claim_id
        if private_claim_id is not None
        else None
    )
    try:
        _reserve(runtime, review_claim_id, f"{lane.casefold()}-review-workspace")
        reserved.append(review_claim_id)
        if lane == "F2":
            if private_claim_id is None:
                _fail("F2 private claim identity is absent")
            _reserve(runtime, private_claim_id, "f2-private-environment")
            reserved.append(private_claim_id)
        for path in (
            review_root / "codex-home",
            review_root / "tool-bin",
            review_root / "staging",
        ):
            path.mkdir(mode=MODE_DIRECTORY)
        codex = review_root / "tool-bin/codex"
        _copy_tool(runtime.codex_path, codex, runtime.codex_sha256)
        _copy_codex_auth(review_root / "codex-home")
        uv_sha: str | None = None
        if private_root is not None:
            base = private_root / "f2-private"
            for name in ("home", "uv-cache", "venv", "tool-bin"):
                (base / name).mkdir(parents=True, mode=MODE_DIRECTORY)
            _copy_tool(runtime.uv_path, base / "tool-bin/uv", runtime.uv_sha256)
            uv_sha = runtime.uv_sha256
        _write_review_manifest(runtime, review_root, inputs_path)
        _activate_empty(runtime.ledger_path, review_claim_id, runtime.controller_root)
        if private_claim_id is not None:
            _activate_empty(
                runtime.ledger_path, private_claim_id, runtime.controller_root
            )
        closure = ToolClosure(runtime.codex_sha256, uv_sha)
        return ReviewFilesystems(
            review_claim_id,
            private_claim_id,
            review_root,
            private_root,
            closure,
        )
    except Exception:
        _rollback_reserved(runtime, reserved)
        raise


def cleanup_review_filesystems(
    runtime: ReviewRuntimeInputs, filesystems: ReviewFilesystems
) -> None:
    """Remove only recorded claim roots and release their exact ledger UUIDs."""
    for claim_id, root in (
        (filesystems.private_claim_id, filesystems.private_root),
        (filesystems.review_claim_id, filesystems.review_root),
    ):
        if claim_id is None or root is None:
            continue
        if root.exists() and not root.is_symlink():
            _remove_private_root(root, runtime.attempt_root / "claims")
        ledger, _ = load_json(runtime.ledger_path)
        if any(
            item.get("claim_id") == claim_id for item in claim_objects(ledger["claims"])
        ):
            release_claim(runtime.ledger_path, claim_id)


def private_tree_sha256(root: Path) -> str:
    """Hash the exact no-symlink private-environment tree after synchronization."""
    records: list[JsonValue] = []
    for path in sorted(root.rglob("*")):
        value = path.lstat()
        if stat.S_ISLNK(value.st_mode):
            _fail("private environment contains a symlink")
        relative = str(path.relative_to(root))
        record: JsonObject = {
            "mode": stat.S_IMODE(value.st_mode),
            "path": relative,
            "type": "directory" if stat.S_ISDIR(value.st_mode) else "file",
        }
        if stat.S_ISREG(value.st_mode):
            record["sha256"] = raw_sha256(path.read_bytes())
        elif not stat.S_ISDIR(value.st_mode):
            _fail("private environment contains a special file")
        records.append(record)
    return raw_sha256(canonical_bytes(records))


def _reserve(runtime: ReviewRuntimeInputs, claim_id: str, purpose: str) -> None:
    spec: JsonObject = {
        "claim_id": claim_id,
        "dependency_claim_ids": [],
        "desired": {"owned_files": [], "published_outputs": []},
        "kind": "filesystem",
        "purpose": purpose,
    }
    path = runtime.controller_root / f"{claim_id}.spec.json"
    write_no_replace(path, canonical_bytes(spec), mode=MODE_IMMUTABLE)
    try:
        _ = reserve_claim(runtime.ledger_path, path)
    finally:
        path.unlink(missing_ok=True)


def _activate_empty(ledger_path: Path, claim_id: str, root: Path) -> None:
    observation: JsonObject = {"owned_files": [], "published_outputs": []}
    path = root / f"{claim_id}.observation.json"
    write_no_replace(path, canonical_bytes(observation), mode=MODE_IMMUTABLE)
    try:
        activate_claim(ledger_path, claim_id, path)
    finally:
        path.unlink(missing_ok=True)


def _copy_tool(source: Path, destination: Path, expected_sha256: str) -> None:
    raw = source.read_bytes()
    if raw_sha256(raw) != expected_sha256:
        _fail("review tool source drifted before materialization")
    write_no_replace(destination, raw, mode=0o555)
    if raw_sha256(destination.read_bytes()) != expected_sha256:
        _fail("review tool copy drifted after materialization")


def _copy_codex_auth(destination: Path) -> None:
    source = Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".codex/auth.json"
    regular_identity(source, mode=MODE_PRIVATE)
    write_no_replace(destination / "auth.json", source.read_bytes(), mode=MODE_PRIVATE)


def _write_review_manifest(
    runtime: ReviewRuntimeInputs, root: Path, inputs_path: Path
) -> None:
    manifest: JsonObject = {
        "approved_plan": str(runtime.approved_plan),
        "approved_sidecar": str(runtime.approved_sidecar),
        "frozen_manifest": str(inputs_path),
        "receipts": [str(path) for path in runtime.receipt_paths],
    }
    path = root / "staging/review-manifest.json"
    write_no_replace(path, canonical_bytes(manifest), mode=MODE_IMMUTABLE)


def _remove_private_root(root: Path, claims_root: Path) -> None:
    if root.parent != claims_root or root.is_symlink():
        _fail("review cleanup root escaped its claim namespace")
    value = root.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(value.st_mode)
        or stat.S_IMODE(value.st_mode) != MODE_DIRECTORY
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
    ):
        _fail("review cleanup root identity drifted")
    shutil.rmtree(root)
    if root.exists() or root.is_symlink():
        _fail("review cleanup did not remove its claim root")


def _rollback_reserved(runtime: ReviewRuntimeInputs, claim_ids: list[str]) -> None:
    for claim_id in reversed(claim_ids):
        root = runtime.attempt_root / "claims" / claim_id
        if root.exists() and not root.is_symlink():
            _remove_private_root(root, runtime.attempt_root / "claims")
        release_claim(runtime.ledger_path, claim_id)


def _fail(message: str) -> Never:
    raise IsolationError(message)
