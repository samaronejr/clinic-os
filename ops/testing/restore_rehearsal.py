"""Execute the disposable PostgreSQL 16.14 logical recovery rehearsal."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Never

from ops.testing.isolation_common import JsonObject, JsonValue, canonical_bytes
from ops.testing.isolation_docker_metadata import run_docker_command
from ops.testing.restore_container import ContainerPostgres, VersionEvidence
from ops.testing.restore_contract import (
    RestoreContractError,
    expected_toc,
    require_exact_toc,
    require_source_scope,
)
from ops.testing.restore_escrow import (
    delete_restored_secrets,
    read_key_escrow,
    require_escrow_opens_source,
    require_object_backup,
    restore_key_escrow,
    restore_object_backup,
)
from ops.testing.restore_purge import (
    CURRENT_TOMBSTONES_SQL,
    reapply_purge_tombstones,
    restore_media_copy,
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
    require_app_role_probes,
    require_audit_chain,
    require_audit_content,
    require_empty_target,
    require_equal_fingerprints,
    require_equal_migration_leaves,
    require_equal_target_seed,
    require_equal_tenant_key_status,
    require_hba,
    require_key_probe,
    require_owner_rls_acl_posture,
    require_sequence_headroom,
    seed_foreign_probe_tenant,
    source_scope,
    verify_object_store,
)

MAX_SECRET_FRAME_BYTES: Final = 4096
PRIVATE_DIRECTORY_MODE: Final = 0o700
PRIVATE_FILE_MODE: Final = 0o600
MIN_PRIVATE_FD: Final = 3
CLAIM_LABEL: Final = "clinic.phase1a.claim"
_CLAIM_INSPECT_FIELDS: Final = 3
CLAIM_ID_PATTERN: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
VERIFY_TIMEOUT_SECONDS: Final = 600
# Plan PF proposals (RPO <= 5 min, RTO <= 60 min including objects and keys);
# they are proposals, compared with what this synthetic drill measured.
RPO_PROPOSAL_SECONDS: Final = 300
RTO_PROPOSAL_SECONDS: Final = 3600


@dataclass(frozen=True, slots=True)
class KeyProbe:
    """Bind one source envelope to its expected plaintext digest.

    The binding never carries key material: the KEK comes only from escrow.
    """

    organization_id: str
    envelope_hex: str
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class RehearsalEvidence:
    """Expose only redacted recovery proofs and explicit non-claims."""

    acknowledged_writes_lost: int
    app_role_probes_verified: bool
    archive_absent: bool
    attachment_objects_verified: int
    audit_chain_verified: bool
    audit_content_verified: int
    claim_binding_verified: bool
    encrypted_provider_backup_verified: bool
    escrow_opens_source: bool
    key_escrow_restored: bool
    logical_restore_only: bool
    no_restored_browser_runner_started: bool
    no_restored_provisioning_command_ran: bool
    normalized_toc: tuple[str, ...]
    object_backup_objects: int
    provider_pitr_verified: bool
    purge_tombstones_reapplied: int
    restored_secrets_absent: bool
    restored_workflows_verified: bool
    rpo_measured_seconds: float
    rto_measured_seconds: float
    sequence_headroom: tuple[tuple[str, int, int], ...]
    source_version: VersionEvidence
    target_version: VersionEvidence
    tenant_key_probe_verified: bool
    tenant_key_status: str


def _probe_restored_app_role(
    target: ContainerPostgres,
    target_app: ContainerPostgres,
    probe: KeyProbe,
) -> None:
    """Exercise the restored runtime role against a populated foreign row.

    Seeding one foreign tenant on the disposable target is what makes the
    probes meaningful: a permissive policy changes the observed counts, and
    the restored tenant's own count must match the privileged count exactly.
    """
    seed_foreign_probe_tenant(target)
    expected_patients = target.sql(
        "SELECT count(*) FROM clinic_app.intake_patient "  # noqa: S608
        "WHERE organization_id = '" + probe.organization_id + "'"
    ).strip()
    if not expected_patients.isdecimal():
        _fail("restored patient count observation is invalid")
    require_app_role_probes(
        target_app,
        organization_id=probe.organization_id,
        expected_patient_count=int(expected_patients),
    )


def run_rehearsal(  # noqa: PLR0913, PLR0915 - one linear rehearsal pipeline
    source: ContainerPostgres,
    target: ContainerPostgres,
    work_dir: Path,
    probe: KeyProbe,
    object_store: Path,
    *,
    source_claim: str,
    target_claim: str,
    key_escrow: Path,
    object_backup: Path,
    target_app: ContainerPostgres | None = None,
    verify_environment: dict[str, str] | None = None,
    probe_path: Path | None = None,
    media_store: Path | None = None,
    source_media_store: Path | None = None,
) -> RehearsalEvidence:
    """Guard, dump, authenticate, restore rows, objects and keys, then erase.

    Claim binding authenticates that both containers are the leased CI
    databases this run created before any mutation; the empty-target gate
    refuses to overwrite a database that already holds domain rows. The KEK
    comes only from ``key_escrow`` and the attachment objects only from
    ``object_backup``; a missing or stale escrow, or a missing object, fails
    before the target is touched (ADR-016).
    """
    if (
        not work_dir.is_absolute()
        or work_dir.is_symlink()
        or not work_dir.is_dir()
        or work_dir.stat().st_mode & 0o777 != PRIVATE_DIRECTORY_MODE
        or source.container_id == target.container_id
        or source.database == target.database
        or source_claim == target_claim
    ):
        _fail("restore rehearsal binding is invalid")
    require_lease_claim(source.container_id, source_claim)
    require_lease_claim(target.container_id, target_claim)
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
    require_equal_target_seed(source, target)
    # The target must be a migrated-but-empty database: restoring into a
    # database that already holds domain rows is never a rehearsal target.
    require_empty_target(target)
    # Keys and objects are recovered with the rows; a gap in either fails here.
    kek = read_key_escrow(key_escrow)
    require_escrow_opens_source(source, probe, kek)
    backup_objects = require_object_backup(source, object_backup)
    source_fingerprints = relation_fingerprints(source)
    sequence_evidence: tuple[tuple[str, int, int], ...] = ()
    key_status = ""
    objects_verified = 0
    audit_chain = False
    audit_content = 0
    key_probe = False
    app_probes = False
    workflows = False
    tombstones = 0
    restored_secrets: Path | None = None
    secrets_absent = True
    try:
        snapshot_started = time.monotonic()
        write_private_archive(archive, source.dump())
        write_hash_sidecar(archive, sidecar)
        verify_hash_sidecar(archive, sidecar)
        archive_bytes = archive.read_bytes()
        source_toc = source.list_archive(archive_bytes)
        target_toc = target.list_archive(archive_bytes)
        require_exact_toc(source_toc)
        require_exact_toc(target_toc)
        verify_hash_sidecar(archive, sidecar)
        current_tombstones = source.sql(CURRENT_TOMBSTONES_SQL)
        restore_started = time.monotonic()
        target.restore(archive_bytes)
        require_equal_fingerprints(source_fingerprints, relation_fingerprints(target))
        restore_object_backup(object_backup, object_store)
        media_binding = None
        if source_media_store is not None:
            if media_store is None:
                _fail("restore media root is invalid")
            media_binding = restore_media_copy(
                source=source_media_store, destination=media_store, work_dir=work_dir
            )
        tombstones = reapply_purge_tombstones(
            target, current_tombstones, media_store, media_binding=media_binding
        )
        require_owner_rls_acl_posture(target)
        require_audit_chain(target)
        audit_chain = True
        audit_content = require_audit_content(target)
        key_status = require_equal_tenant_key_status(source, target)
        restored_secrets = restore_key_escrow(kek, work_dir)
        require_key_probe(
            target,
            organization_id=probe.organization_id,
            kek=kek,
            envelope_hex=probe.envelope_hex,
            expected_sha256=probe.expected_sha256,
        )
        key_probe = True
        objects_verified = verify_object_store(target, object_store, kek)
        sequence_evidence = require_sequence_headroom(target)
        if target_app is not None:
            _probe_restored_app_role(target, target_app, probe)
            app_probes = True
            if verify_environment is not None and probe_path is not None:
                _run_verify(
                    {**verify_environment, "CLINIC_SECRET_DIR": str(restored_secrets)},
                    probe_path,
                )
                workflows = True
        # Writers were stopped: nothing acknowledged after the recovery point.
        if relation_fingerprints(source) != source_fingerprints:
            _fail("restore drill failed: the source changed after the recovery point")
        verified = time.monotonic()
    finally:
        delete_private_artifacts(archive, sidecar)
        if restored_secrets is not None:
            secrets_absent = delete_restored_secrets(restored_secrets)
    return RehearsalEvidence(
        acknowledged_writes_lost=0,
        app_role_probes_verified=app_probes,
        archive_absent=not archive.exists() and not sidecar.exists(),
        attachment_objects_verified=objects_verified,
        audit_chain_verified=audit_chain,
        audit_content_verified=audit_content,
        claim_binding_verified=True,
        encrypted_provider_backup_verified=False,
        escrow_opens_source=True,
        key_escrow_restored=key_probe,
        logical_restore_only=True,
        no_restored_browser_runner_started=True,
        no_restored_provisioning_command_ran=True,
        normalized_toc=expected_toc(),
        object_backup_objects=backup_objects,
        provider_pitr_verified=False,
        purge_tombstones_reapplied=tombstones,
        restored_secrets_absent=secrets_absent,
        restored_workflows_verified=workflows,
        # The data-loss window: any write after the snapshot until the restore
        # began would be lost; writers were stopped, so none was.
        rpo_measured_seconds=restore_started - snapshot_started,
        rto_measured_seconds=verified - restore_started,
        sequence_headroom=sequence_evidence,
        source_version=source_version,
        target_version=target_version,
        tenant_key_probe_verified=key_probe,
        tenant_key_status=key_status,
    )


def require_lease_claim(container_id: str, claim_id: str) -> None:
    """Authenticate that the container is the leased database for this claim.

    The container must carry the claim label, live under the deterministic
    lease name derived from the claim token, and mount the claim-labeled
    pgdata volume. Anything else is not a rehearsal target and is refused
    before any mutation.
    """
    if CLAIM_ID_PATTERN.fullmatch(claim_id) is None:
        _fail("restore lease claim is invalid")
    token = claim_id.split("-", 1)[0]
    expected_name = f"clinic_phase1a_ci_{token}-database-1"
    expected_volume = f"clinic_phase1a_ci_{token}_pgdata"
    raw = run_docker_command(
        (
            "inspect",
            "--format",
            "{{json .Config.Labels}}|{{.Name}}|"
            '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}',
            container_id,
        )
    ).strip()
    fields = raw.split("|", 2)
    if len(fields) != _CLAIM_INSPECT_FIELDS:
        _fail("restore lease claim inspection is invalid")
    try:
        labels: object = json.loads(fields[0])
    except json.JSONDecodeError:
        _fail("restore lease claim inspection is invalid")
    if not isinstance(labels, dict) or labels.get(CLAIM_LABEL) != claim_id:
        _fail("restore target is not the claimed lease container")
    if fields[1] != f"/{expected_name}":
        _fail("restore target container name does not match the claim")
    # The lease also mounts the shared TLS materializer volume; the claim
    # binds the pgdata volume specifically, so require membership not
    # exclusivity.
    volumes = fields[2].split()
    if expected_volume not in volumes:
        _fail("restore target volume does not match the claim")
    volume_labels_raw = run_docker_command(
        ("volume", "inspect", "--format", "{{json .Labels}}", expected_volume)
    ).strip()
    try:
        volume_labels: object = json.loads(volume_labels_raw)
    except json.JSONDecodeError:
        _fail("restore lease claim inspection is invalid")
    if (
        not isinstance(volume_labels, dict)
        or volume_labels.get(CLAIM_LABEL) != claim_id
    ):
        _fail("restore target volume is not the claimed lease volume")


def _run_verify(environment: dict[str, str], probe_path: Path) -> None:
    """Run the Django verify step against the restored target as clinic_app."""
    asyncio.run(_run_verify_async(environment, probe_path))


async def _run_verify_async(environment: dict[str, str], probe_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ops.testing.restore_fixture",
        "verify",
        "--probe-path",
        str(probe_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=VERIFY_TIMEOUT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        _fail("restored workflow verification timed out")
    if process.returncode != 0:
        detail = (stdout + stderr).decode("utf-8", errors="replace")[-2000:]
        _fail(f"restored workflow verification failed: {detail}")


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
    probe = _read_probe(Path(arguments.probe))
    object_store = Path(arguments.object_store)
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
    target_app = target.for_role("clinic_app", credentials["target_app_password"])
    verify_environment = _verify_environment(arguments, credentials["target_app_url"])
    started = time.monotonic()
    evidence = run_rehearsal(
        source,
        target,
        work_dir,
        probe,
        object_store,
        source_claim=arguments.source_claim,
        target_claim=arguments.target_claim,
        target_app=target_app,
        key_escrow=Path(arguments.key_escrow),
        object_backup=Path(arguments.object_backup),
        verify_environment=verify_environment,
        probe_path=Path(arguments.probe),
        media_store=Path(arguments.media_store),
        source_media_store=Path(arguments.source_media_store),
    )
    elapsed = time.monotonic() - started
    value: JsonObject = {
        **_evidence_json(evidence),
        "elapsed_seconds": elapsed,
        "receipt_statement": (
            "logical restore only; provider PITR not verified; "
            "encrypted provider backup not verified"
        ),
        "schema_version": 2,
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
            "target_app_password",
            "target_app_url",
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


def _read_probe(path: Path) -> KeyProbe:
    """Read the seed-published probe binding from the private work directory."""
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        _fail("key probe binding path is invalid")
    if path.stat().st_mode & 0o777 != PRIVATE_FILE_MODE:
        _fail("key probe binding must be private")
    try:
        value: object = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        _fail(f"key probe binding is invalid: {error}")
    required = {
        "envelope_hex",
        "expected_sha256",
        "organization_id",
    }
    allowed = required | {
        "attachment_id",
        "clinic_id",
        "clinical_document_id",
        "invoice_id",
        "owner_id",
        "patient_name",
        "patient_session_id",
        "physician_id",
        "result_version_id",
        "version_id",
    }
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or not set(value) <= allowed
    ):
        _fail("key probe binding is invalid")
    fields: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            _fail("key probe binding is invalid")
        fields[key] = item
    return KeyProbe(
        organization_id=fields["organization_id"],
        envelope_hex=fields["envelope_hex"],
        expected_sha256=fields["expected_sha256"],
    )


def _verify_environment(
    arguments: argparse.Namespace, app_url: str
) -> dict[str, str] | None:
    """Build the closed environment for the app-role verify subprocess.

    The rehearsal adds ``CLINIC_SECRET_DIR`` itself, pointing at the secret
    store it restored from escrow, never at the source's live store.
    """
    attachment_root = arguments.attachment_root
    if attachment_root is None:
        return None
    return {
        "CLINIC_SECRET_BACKEND": "synthetic-file",
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "EHR_ATTACHMENT_ROOT": attachment_root,
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MIGRATION_DATABASE_URL": app_url,
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
    }


def _evidence_json(evidence: RehearsalEvidence) -> JsonObject:
    source = asdict(evidence.source_version)
    target = asdict(evidence.target_version)
    sequences: list[JsonValue] = [
        {"maximum": maximum, "name": name, "next_value": next_value}
        for name, next_value, maximum in evidence.sequence_headroom
    ]
    objectives: JsonObject = {
        "acknowledged_writes_lost": evidence.acknowledged_writes_lost,
        "basis": "synthetic logical drill, local L1 evidence; targets are proposals",
        "rpo_measured_seconds": evidence.rpo_measured_seconds,
        "rpo_proposal_seconds": RPO_PROPOSAL_SECONDS,
        "rpo_within_proposal": evidence.rpo_measured_seconds <= RPO_PROPOSAL_SECONDS,
        "rto_measured_seconds": evidence.rto_measured_seconds,
        "rto_proposal_seconds": RTO_PROPOSAL_SECONDS,
        "rto_within_proposal": evidence.rto_measured_seconds <= RTO_PROPOSAL_SECONDS,
    }
    return {
        "app_role_probes_verified": evidence.app_role_probes_verified,
        "archive_absent": evidence.archive_absent,
        "attachment_objects_verified": evidence.attachment_objects_verified,
        "audit_chain_verified": evidence.audit_chain_verified,
        "audit_content_verified": evidence.audit_content_verified,
        "claim_binding_verified": evidence.claim_binding_verified,
        "encrypted_provider_backup_verified": (
            evidence.encrypted_provider_backup_verified
        ),
        "escrow_opens_source": evidence.escrow_opens_source,
        "key_escrow_restored": evidence.key_escrow_restored,
        "logical_restore_only": evidence.logical_restore_only,
        "no_restored_browser_runner_started": (
            evidence.no_restored_browser_runner_started
        ),
        "no_restored_provisioning_command_ran": (
            evidence.no_restored_provisioning_command_ran
        ),
        "normalized_toc": list(evidence.normalized_toc),
        "object_backup_objects": evidence.object_backup_objects,
        "provider_pitr_verified": evidence.provider_pitr_verified,
        "purge_tombstones_reapplied": evidence.purge_tombstones_reapplied,
        "recovery_objectives": objectives,
        "restored_secrets_absent": evidence.restored_secrets_absent,
        "restored_workflows_verified": evidence.restored_workflows_verified,
        "sequence_headroom": sequences,
        "source_version": source,
        "target_version": target,
        "tenant_key_probe_verified": evidence.tenant_key_probe_verified,
        "tenant_key_status": evidence.tenant_key_status,
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
    parser.add_argument("--probe", required=True)
    parser.add_argument("--object-store", required=True)
    parser.add_argument("--object-backup", required=True)
    parser.add_argument("--key-escrow", required=True)
    parser.add_argument("--media-store", required=True)
    parser.add_argument("--source-media-store", required=True)
    parser.add_argument("--source-claim", required=True)
    parser.add_argument("--target-claim", required=True)
    parser.add_argument("--attachment-root")
    return parser


def _fail(message: str) -> Never:
    raise RestoreContractError(message)


if __name__ == "__main__":
    raise SystemExit(main())
