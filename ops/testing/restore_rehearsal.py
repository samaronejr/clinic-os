"""Execute the disposable PostgreSQL 16.14 logical recovery rehearsal."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Never

from ops.testing.isolation_common import JsonObject, JsonValue, canonical_bytes
from ops.testing.restore_container import ContainerPostgres, VersionEvidence
from ops.testing.restore_contract import (
    RestoreContractError,
    expected_toc,
    require_exact_toc,
    require_source_scope,
)
from ops.testing.restore_transport import (
    delete_private_artifacts,
    verify_hash_sidecar,
    write_hash_sidecar,
    write_private_archive,
)
from ops.testing.restore_verification import (
    migration_leaves,
    relation_fingerprints,
    require_equal_fingerprints,
    require_equal_migration_leaves,
    require_hba,
    require_owner_rls_acl_posture,
    require_sequence_headroom,
    source_scope,
)

MAX_SECRET_FRAME_BYTES: Final = 4096
PRIVATE_DIRECTORY_MODE: Final = 0o700
MIN_PRIVATE_FD: Final = 3


@dataclass(frozen=True, slots=True)
class RehearsalEvidence:
    """Expose only redacted recovery proofs and explicit non-claims."""

    archive_absent: bool
    encrypted_provider_backup_verified: bool
    logical_restore_only: bool
    no_restored_browser_runner_started: bool
    no_restored_provisioning_command_ran: bool
    normalized_toc: tuple[str, ...]
    provider_pitr_verified: bool
    sequence_headroom: tuple[tuple[str, int, int], ...]
    source_version: VersionEvidence
    target_version: VersionEvidence


def run_rehearsal(
    source: ContainerPostgres,
    target: ContainerPostgres,
    work_dir: Path,
) -> RehearsalEvidence:
    """Guard, dump, authenticate, restore, compare, and erase one archive."""
    if (
        not work_dir.is_absolute()
        or work_dir.is_symlink()
        or not work_dir.is_dir()
        or work_dir.stat().st_mode & 0o777 != PRIVATE_DIRECTORY_MODE
        or source.container_id == target.container_id
        or source.database == target.database
    ):
        _fail("restore rehearsal binding is invalid")
    archive = work_dir / "logical-recovery.dump"
    sidecar = work_dir / "logical-recovery.dump.sha256"
    source_version = source.require_postgresql_16_14()
    target_version = target.require_postgresql_16_14()
    require_hba(source)
    require_hba(target)
    observed_scope = source_scope(source)
    require_source_scope(observed_scope)
    source_leaves = migration_leaves(source)
    target_leaves = migration_leaves(target)
    require_equal_migration_leaves(source_leaves, target_leaves)
    source_fingerprints = relation_fingerprints(source)
    sequence_evidence: tuple[tuple[str, int, int], ...] = ()
    try:
        write_private_archive(archive, source.dump())
        write_hash_sidecar(archive, sidecar)
        verify_hash_sidecar(archive, sidecar)
        archive_bytes = archive.read_bytes()
        source_toc = source.list_archive(archive_bytes)
        target_toc = target.list_archive(archive_bytes)
        require_exact_toc(source_toc)
        require_exact_toc(target_toc)
        verify_hash_sidecar(archive, sidecar)
        target.restore(archive_bytes)
        require_equal_fingerprints(source_fingerprints, relation_fingerprints(target))
        require_owner_rls_acl_posture(target)
        sequence_evidence = require_sequence_headroom(target)
    finally:
        delete_private_artifacts(archive, sidecar)
    return RehearsalEvidence(
        archive_absent=not archive.exists() and not sidecar.exists(),
        encrypted_provider_backup_verified=False,
        logical_restore_only=True,
        no_restored_browser_runner_started=True,
        no_restored_provisioning_command_ran=True,
        normalized_toc=expected_toc(),
        provider_pitr_verified=False,
        sequence_headroom=sequence_evidence,
        source_version=source_version,
        target_version=target_version,
    )


def main() -> int:
    """Parse the closed task-owned container form and publish redacted evidence."""
    arguments = _parser().parse_args()
    work_dir = Path(arguments.work_dir)
    evidence_path = Path(arguments.evidence)
    if (
        not evidence_path.is_absolute()
        or evidence_path.exists()
        or evidence_path.is_symlink()
    ):
        _fail("restore rehearsal evidence path is invalid")
    credentials = _read_credentials(arguments.credentials_fd)
    source = ContainerPostgres(
        arguments.source_container,
        arguments.source_database,
        credentials["source_password"],
    )
    target = ContainerPostgres(
        arguments.target_container,
        arguments.target_database,
        credentials["target_password"],
    )
    started = time.monotonic()
    evidence = run_rehearsal(source, target, work_dir)
    elapsed = time.monotonic() - started
    value: JsonObject = {
        **_evidence_json(evidence),
        "elapsed_seconds": elapsed,
        "receipt_statement": (
            "logical restore only; provider PITR not verified; "
            "encrypted provider backup not verified"
        ),
        "schema_version": 1,
    }
    _publish_evidence(evidence_path, canonical_bytes(value))
    return 0


def _read_credentials(descriptor: int) -> dict[str, str]:
    if descriptor < MIN_PRIVATE_FD:
        _fail("credential descriptor must be private")
    frame = bytearray(os.read(descriptor, MAX_SECRET_FRAME_BYTES + 1))
    try:
        if (
            not frame
            or len(frame) > MAX_SECRET_FRAME_BYTES
            or not frame.endswith(b"\n")
        ):
            _fail("credential frame is invalid")
        value: object = json.loads(frame)
        if not isinstance(value, dict) or set(value) != {
            "source_password",
            "target_password",
        }:
            _fail("credential frame is invalid")
        credentials: dict[str, str] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str) or not item:
                _fail("credential frame is invalid")
            credentials[key] = item
        return credentials
    finally:
        for index in range(len(frame)):
            frame[index] = 0


def _evidence_json(evidence: RehearsalEvidence) -> JsonObject:
    source = asdict(evidence.source_version)
    target = asdict(evidence.target_version)
    sequences: list[JsonValue] = [
        {"maximum": maximum, "name": name, "next_value": next_value}
        for name, next_value, maximum in evidence.sequence_headroom
    ]
    return {
        "archive_absent": evidence.archive_absent,
        "encrypted_provider_backup_verified": (
            evidence.encrypted_provider_backup_verified
        ),
        "logical_restore_only": evidence.logical_restore_only,
        "no_restored_browser_runner_started": (
            evidence.no_restored_browser_runner_started
        ),
        "no_restored_provisioning_command_ran": (
            evidence.no_restored_provisioning_command_ran
        ),
        "normalized_toc": list(evidence.normalized_toc),
        "provider_pitr_verified": evidence.provider_pitr_verified,
        "sequence_headroom": sequences,
        "source_version": source,
        "target_version": target,
    }


def _publish_evidence(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--source-container", required=True)
    parser.add_argument("--target-container", required=True)
    parser.add_argument("--source-database", required=True)
    parser.add_argument("--target-database", required=True)
    parser.add_argument("--credentials-fd", type=int, required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--evidence", required=True)
    return parser


def _fail(message: str) -> Never:
    raise RestoreContractError(message)


if __name__ == "__main__":
    raise SystemExit(main())
