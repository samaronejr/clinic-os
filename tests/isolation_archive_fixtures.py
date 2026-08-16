from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

import rfc8785
from ops.testing.isolation_archive import ArchiveRequest
from ops.testing.isolation_close import close_rejected_attempt
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_no_replace,
)
from ops.testing.isolation_rejection import RejectionRequest, reject_attempt
from ops.testing.isolation_user_fix import (
    UserFixAuthorizationRequest,
    authorize_user_fix,
    build_user_rejection_context,
)

from isolation_claim_fixtures import FOUNDATION_SHA, snapshot
from isolation_rejection_fixtures import (
    FailureReceiptSpec,
    empty_inventory,
    write_failure_receipt,
)
from isolation_user_fixtures import user_gate_fixture


@dataclass(frozen=True, slots=True)
class ClosedArchiveFixture:
    ledger_path: Path
    control_root: Path
    attempt_root: Path
    request: ArchiveRequest
    control_tree_sha256: str
    attempt_tree_sha256: str
    ledger_sha256: str
    seed_sha256: str
    validation_sha256: str
    lineage_sha256: str


def closed_archive_fixture(tmp_path: Path) -> ClosedArchiveFixture:
    ledger_path = snapshot(tmp_path)
    ledger, _ = load_json(ledger_path)
    attempt_root = Path(str(ledger["attempt_root"]))
    control_root = ledger_path.parent / "clinic-os-phase1a-final"
    terminal = control_root / "terminal"
    terminal.mkdir(mode=0o700, parents=True)
    write_no_replace(
        terminal / "F1-verdict.json",
        canonical_bytes({"verdict": "REJECT"}),
        mode=MODE_IMMUTABLE,
    )
    seed_sha, validation_sha, lineage_sha = _publish_lineage(
        attempt_root,
        str(ledger["attempt_id"]),
    )
    control_sha = tree_sha256(control_root)
    write_failure_receipt(
        ledger_path,
        FailureReceiptSpec(
            lane="F1",
            cause_code="reviewer-reject",
            failure_class="finding",
            stage="F1-review",
            control_tree_sha256=control_sha,
            lineage_validation_sha256=validation_sha,
        ),
    )
    rejection_sha = reject_attempt(
        ledger_path,
        RejectionRequest(
            sha=FOUNDATION_SHA,
            reasons=("F1:finding",),
            inputs_sha256=None,
            pre_f4_sha256=None,
            final_sha256=None,
        ),
        inventory_reader=empty_inventory,
    )
    close_rejected_attempt(ledger_path, inventory_reader=empty_inventory)
    attempt_sha = tree_sha256(attempt_root)
    ledger_sha = raw_sha256(ledger_path.read_bytes())
    request = ArchiveRequest(
        closed_attempt=str(ledger["attempt_id"]),
        rejected_sha=FOUNDATION_SHA,
        control_root=control_root,
        rejection_spec_sha256=rejection_sha,
    )
    return ClosedArchiveFixture(
        ledger_path=ledger_path,
        control_root=control_root,
        attempt_root=attempt_root,
        request=request,
        control_tree_sha256=control_sha,
        attempt_tree_sha256=attempt_sha,
        ledger_sha256=ledger_sha,
        seed_sha256=seed_sha,
        validation_sha256=validation_sha,
        lineage_sha256=lineage_sha,
    )


def closed_user_archive_fixture(tmp_path: Path) -> ClosedArchiveFixture:
    fixture = user_gate_fixture(tmp_path)
    ledger, _ = load_json(fixture.ledger_path)
    attempt_root = Path(str(ledger["attempt_root"]))
    seed_sha, validation_sha, lineage_sha = _publish_lineage(
        attempt_root,
        str(ledger["attempt_id"]),
    )
    binding = authorize_user_fix(
        fixture.ledger_path,
        UserFixAuthorizationRequest(
            FOUNDATION_SHA,
            fixture.terminal_path,
            fixture.control_root,
        ),
        inventory_reader=empty_inventory,
    )
    context = build_user_rejection_context(
        fixture.ledger_path,
        sha=FOUNDATION_SHA,
        control_root=fixture.control_root,
        user_authorization=binding,
        inventory_reader=empty_inventory,
    )
    rejection_sha = reject_attempt(
        fixture.ledger_path,
        RejectionRequest(
            FOUNDATION_SHA,
            ("USER:user-requested-fix",),
            context[1],
            context[2],
            context[3],
            binding,
        ),
        inventory_reader=empty_inventory,
    )
    close_rejected_attempt(
        fixture.ledger_path,
        inventory_reader=empty_inventory,
        user_authorization=binding,
    )
    return ClosedArchiveFixture(
        ledger_path=fixture.ledger_path,
        control_root=fixture.control_root,
        attempt_root=attempt_root,
        request=ArchiveRequest(
            closed_attempt=str(ledger["attempt_id"]),
            rejected_sha=FOUNDATION_SHA,
            control_root=fixture.control_root,
            rejection_spec_sha256=rejection_sha,
        ),
        control_tree_sha256=tree_sha256(fixture.control_root),
        attempt_tree_sha256=tree_sha256(attempt_root),
        ledger_sha256=raw_sha256(fixture.ledger_path.read_bytes()),
        seed_sha256=seed_sha,
        validation_sha256=validation_sha,
        lineage_sha256=lineage_sha,
    )


def tree_sha256(root: Path) -> str:
    entries: list[JsonValue] = []
    _walk_tree(root, root, entries)
    return raw_sha256(rfc8785.dumps(entries))


def _walk_tree(root: Path, path: Path, entries: list[JsonValue]) -> None:
    value = path.lstat()
    relative = "." if path == root else path.relative_to(root).as_posix()
    record: JsonObject = {
        "gid": value.st_gid,
        "link_count": value.st_nlink,
        "mode": stat.S_IMODE(value.st_mode),
        "relative_path": relative,
        "uid": value.st_uid,
    }
    if stat.S_ISDIR(value.st_mode):
        record["type"] = "directory"
        entries.append(record)
        for child in sorted(path.iterdir(), key=lambda item: item.name.encode()):
            _walk_tree(root, child, entries)
        return
    if stat.S_ISREG(value.st_mode):
        raw = path.read_bytes()
        record.update(
            {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "type": "regular",
            }
        )
        entries.append(record)
        return
    if stat.S_ISLNK(value.st_mode):
        record.update({"link_target": str(path.readlink()), "type": "symlink"})
        entries.append(record)
        return
    message = f"unsupported fixture tree entry: {path}"
    raise AssertionError(message)


def _publish_lineage(attempt_root: Path, attempt_id: str) -> tuple[str, str, str]:
    seed_path = attempt_root / "receipt-lineage-seed.json"
    if not seed_path.exists():
        seed: JsonObject = {
            "current_attempt_id": attempt_id,
            "fix_sources": [],
            "next_fix_sequence": 1,
            "previous_attempt_id": None,
            "previous_bundle_path": None,
            "previous_lineage_sha256": None,
            "previous_tombstone_sha256": None,
            "primary_sources": [],
            "schema_version": 1,
        }
        write_no_replace(seed_path, canonical_bytes(seed), mode=MODE_IMMUTABLE)
    seed_sha = raw_sha256(seed_path.read_bytes())
    common: JsonObject = {
        "current_attempt_id": attempt_id,
        "fix_sources": [],
        "next_fix_sequence": 1,
        "primary_sources": [],
        "schema_version": 1,
        "seed_sha256": seed_sha,
    }
    validation_path = attempt_root / "receipt-lineage-validation.json"
    write_no_replace(validation_path, canonical_bytes(common), mode=MODE_IMMUTABLE)
    validation_sha = raw_sha256(validation_path.read_bytes())
    lineage = dict(common)
    lineage["lineage_validation_sha256"] = validation_sha
    lineage_path = attempt_root / "receipt-lineage.json"
    write_no_replace(lineage_path, canonical_bytes(lineage), mode=MODE_IMMUTABLE)
    return seed_sha, validation_sha, raw_sha256(lineage_path.read_bytes())
