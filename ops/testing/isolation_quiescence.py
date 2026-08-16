"""Revalidate the claim-free namespace, evidence, and host baseline."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Never

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    JsonValue,
    directory_identity,
    load_json,
    raw_sha256,
    regular_identity,
)
from ops.testing.isolation_host_state import require_known_host_state
from ops.testing.shared_evidence_baseline import (
    DEFAULT_ALLOWANCES,
    ManifestAllowances,
    verify_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def require_quiescent_attempt(
    ledger_path: Path,
    ledger: JsonObject,
    inventory_reader: Callable[[], JsonObject],
    *,
    attempt_root_override: Path | None = None,
    manifest_allowances: ManifestAllowances = DEFAULT_ALLOWANCES,
) -> None:
    """Prove zero claims and equality with every persisted ambient baseline."""
    if ledger.get("claims") != []:
        _fail("rejection requires zero claims")
    require_known_host_state(ledger, inventory_reader())
    _verify_namespace(ledger_path, ledger)
    _verify_shared_evidence(
        ledger_path,
        ledger,
        attempt_root_override,
        manifest_allowances,
    )
    _verify_approved_plan(ledger_path, ledger)


def _verify_namespace(ledger_path: Path, ledger: JsonObject) -> None:
    binding = _object(ledger.get("authority_binding"), "authority binding")
    worktree = Path(_text(ledger.get("worktree_realpath"), "worktree realpath"))
    link_path = worktree / ".omo"
    if _text(binding.get("worktree_omo_path"), "worktree .omo path") != str(link_path):
        _fail("worktree authority path changed")
    expected = _object(binding.get("worktree_omo_lstat"), "worktree .omo identity")
    value = link_path.lstat()
    if not stat.S_ISLNK(value.st_mode):
        _fail("worktree authority binding is not a symlink")
    actual: JsonObject = {
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "link_target": str(link_path.readlink()),
        "mode": stat.S_IMODE(value.st_mode),
        "realpath": str(link_path.resolve(strict=True)),
        "type": "symlink-to-directory",
        "uid": value.st_uid,
    }
    if actual != expected:
        _fail("worktree authority binding drifted")
    root = Path(_text(binding.get("authority_root_realpath"), "authority root"))
    if root != ledger_path.parent.parent or link_path.resolve(strict=True) != root:
        _fail("ledger escaped its authenticated authority root")
    expected_root = _object(
        binding.get("authority_root_identity"),
        "authority root identity",
    )
    observed_root = directory_identity(root)
    observed_root.pop("link_count")
    if observed_root != expected_root:
        _fail("authority root identity drifted")


def _verify_shared_evidence(
    ledger_path: Path,
    ledger: JsonObject,
    attempt_root_override: Path | None,
    manifest_allowances: ManifestAllowances,
) -> None:
    baseline = _object(ledger.get("baseline"), "ledger baseline")
    record = _object(
        baseline.get("shared_evidence_manifest"),
        "shared evidence manifest record",
    )
    attempt_root = Path(_text(ledger.get("attempt_root"), "attempt root"))
    recorded_path = attempt_root / "shared-evidence-baseline.json"
    if _text(record.get("path"), "shared evidence manifest path") != str(recorded_path):
        _fail("shared evidence manifest path changed")
    path = (
        recorded_path
        if attempt_root_override is None
        else attempt_root_override / recorded_path.name
    )
    regular_identity(path, mode=MODE_IMMUTABLE)
    manifest, raw = load_json(path)
    if raw_sha256(raw) != record.get("sha256"):
        _fail("shared evidence manifest hash drifted")
    if manifest.get("entry_count") != record.get("entry_count"):
        _fail("shared evidence manifest count drifted")
    verify_manifest(
        ledger_path.parent,
        manifest,
        attempt_id=_text(ledger.get("attempt_id"), "attempt ID"),
        allowances=manifest_allowances,
    )


def _verify_approved_plan(ledger_path: Path, ledger: JsonObject) -> None:
    plan = _object(ledger.get("approved_plan"), "approved plan")
    path = Path(_text(plan.get("path"), "approved plan path"))
    sidecar = Path(_text(plan.get("sidecar_path"), "approved plan sidecar"))
    expected_path = ledger_path.parent / "review-inputs" / "approved-plan.md"
    if path != expected_path or sidecar != expected_path.with_suffix(".sha256"):
        _fail("approved plan path changed")
    regular_identity(path, mode=MODE_IMMUTABLE)
    regular_identity(sidecar, mode=MODE_PRIVATE)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != plan.get("sha256") or sidecar.read_bytes() != f"{digest}\n".encode():
        _fail("approved plan bytes or sidecar drifted")


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _fail(message: str) -> Never:
    raise IsolationError(message)
