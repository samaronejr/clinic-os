"""Create two claimed TLS databases and drive the disposable recovery exercise.

The exercise seeds a synthetic source, rotates its tenant DEK and KEK the way
an operator would, escrows the rotated KEK and backs up the attachment objects,
then proves three failure variants refuse before the target is touched (no
KEK escrow, a stale pre-rotation escrow, an object missing from the backup)
before one full restore of rows, objects and keys. Stdout carries the exercise
report validated by ``docs/operations/recovery-exercise.schema.json``.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Never

from ops.testing.ci_postgres_runtime import (
    CiDatabaseLease,
    ci_database_lease,
    database_url,
)
from ops.testing.isolation_common import (
    JsonObject,
    JsonValue,
    canonical_bytes,
    ensure_private_directory,
    load_json,
    utc_now,
)
from ops.testing.restore_escrow import (
    KEK_SECRET_FILE,
    MISSING_ESCROW,
    STALE_ESCROW,
)
from ops.testing.runtime_paths import runtime_directory
from ops.testing.tls_export import public_ca_export
from ops.testing.tls_materializer import materializer_lease

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
PYTHON: Final = Path(sys.executable)
UV: Final = Path(shutil.which("uv") or "")
GIT: Final = Path("/usr/bin/git")
MISSING_OBJECT: Final = "attachment object(s) are missing from the object backup"
RECEIPT_STATEMENT: Final = (
    "logical restore only; provider PITR not verified; "
    "encrypted provider backup not verified"
)


def main(repository: Path | None = None) -> int:
    """Run target-first migration, seed, rotation, failure variants and restore.

    ``repository`` locates only the isolation ledger; a QA caller passes a
    root holding a fresh same-boot ledger so the retained prior-boot ledger
    is never claimed or mutated.
    """
    if not UV.is_absolute() or not UV.is_file():
        _fail("restore acceptance uv launcher is unavailable")
    root = PROJECT_ROOT if repository is None else repository
    ledger_path = (root / ".omo/evidence/isolation-ledger-phase1a.json").resolve(
        strict=True
    )
    run_root = _run_root(ledger_path)
    started_at = utc_now()
    with (
        materializer_lease(root) as materializer,
        public_ca_export(materializer) as public_ca,
        ci_database_lease(root, materializer, public_ca, "source") as source,
        ci_database_lease(root, materializer, public_ca, "restore") as target,
        runtime_directory(run_root, purpose="restore") as work_dir,
    ):
        _bootstrap_database(target)
        _migrate(target)
        _bootstrap_database(source)
        _migrate(source)
        secret_dir = _secret_dir(work_dir)
        attachment_source = work_dir / "attachments-source"
        attachment_source.mkdir(mode=0o700)
        (work_dir / "media-source").mkdir(mode=0o700)
        _seed(source, work_dir, secret_dir, attachment_source)
        stale_kek = (secret_dir / KEK_SECRET_FILE).read_text(encoding="ascii")
        rotation = _rotate(source, secret_dir)
        # Backup time: escrow the rotated KEK and copy the object store.
        key_escrow = _escrow(work_dir / "key-escrow", secret_dir)
        object_backup = work_dir / "object-backup"
        shutil.copytree(attachment_source, object_backup)
        restored = work_dir / "attachments-restored"
        restored.mkdir(mode=0o700)
        inputs = RestoreInputs(
            work_dir / "probe.json", restored, key_escrow, object_backup
        )
        variants = _failure_variants(source, target, work_dir, inputs, stale_kek)
        evidence = _run_restore(source, target, work_dir, inputs)
        report: JsonObject = {
            "drill": json.loads(evidence),
            "evidence_level": "L1",
            "exercise": "synthetic-logical-recovery",
            "failure_variants": variants,
            "finished_at": utc_now(),
            "key_rotation": {**rotation, "rotated_before_backup": True},
            "receipt_statement": RECEIPT_STATEMENT,
            "schema_version": 1,
            "source_revision": _source_revision(),
            "started_at": started_at,
            "synthetic": True,
            "verdict": "passed",
        }
        sys.stdout.buffer.write(canonical_bytes(report))
    return 0


@dataclass(frozen=True, slots=True)
class RestoreInputs:
    """The probe binding and the recovery inputs of one restore attempt."""

    probe: Path
    object_store: Path
    key_escrow: Path
    object_backup: Path


def _secret_dir(work_dir: Path) -> Path:
    """Provision the synthetic-file secret backend with one tenant KEK."""
    root = work_dir / "secrets"
    root.mkdir(mode=0o700)
    kek = root / "tenant-kek.secret"
    descriptor = os.open(kek, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, secrets.token_hex(32).encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return root


def _run_root(ledger_path: Path) -> Path:
    """Provision this caller's private run root under the attempt root."""
    ledger, _ = load_json(ledger_path)
    attempt_root = ledger.get("attempt_root")
    if not isinstance(attempt_root, str) or not attempt_root:
        _fail("ledger attempt root is invalid")
    root = Path(attempt_root) / "runtime"
    ensure_private_directory(root)
    return root


def _bootstrap_database(lease: CiDatabaseLease) -> None:
    environment = _base_environment()
    environment.update(
        {
            "CLINIC_APP_PASSWORD": lease.app_password,
            "CLINIC_OWNER_PASSWORD": lease.owner_password,
            "CLINIC_SUPER_PASSWORD": lease.super_password,
            "POSTGRES_CONTAINER": lease.container_id,
            "POSTGRES_DB": lease.database,
            "POSTGRES_PASSWORD": lease.postgres_password,
            "POSTGRES_PORT": "5432",
            "POSTGRES_USER": "postgres",
            "TEST_DATABASE_NAME": f"test_{lease.database}",
        }
    )
    _run(("/usr/bin/make", "db-bootstrap"), environment)


def _migrate(lease: CiDatabaseLease) -> None:
    owner_url = database_url("clinic_owner", lease.owner_password, lease)
    environment = _base_environment()
    environment.update(
        {
            "APP_DATABASE_URL": owner_url,
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "MIGRATION_DATABASE_URL": owner_url,
        }
    )
    _run((str(PYTHON), "manage.py", "migrate", "--noinput"), environment)


def _seed(
    lease: CiDatabaseLease,
    work_dir: Path,
    secret_dir: Path,
    attachment_root: Path,
) -> None:
    owner_environment = _base_environment()
    owner_environment.update(
        {
            "MIGRATION_DATABASE_URL": database_url(
                "clinic_owner", lease.owner_password, lease
            ),
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
        }
    )
    read_descriptor, write_descriptor = os.pipe()
    try:
        os.write(write_descriptor, lease.super_password.encode("utf-8") + b"\n")
        os.close(write_descriptor)
        write_descriptor = -1
        _run(
            (
                str(PYTHON),
                "-m",
                "ops.testing.restore_fixture",
                "bootstrap",
                "--password-fd",
                str(read_descriptor),
            ),
            owner_environment,
            (read_descriptor,),
        )
    finally:
        os.close(read_descriptor)
        if write_descriptor >= 0:
            os.close(write_descriptor)
    app_environment = _base_environment()
    app_environment.update(
        {
            "MIGRATION_DATABASE_URL": database_url(
                "clinic_app", lease.app_password, lease
            ),
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
        }
    )
    _run(
        (str(PYTHON), "-m", "ops.testing.restore_fixture", "totp"),
        app_environment,
    )
    seed_environment = _base_environment()
    seed_environment.update(
        {
            "BILLING_SYNTHETIC_PIX": "1",
            "BILLING_SYNTHETIC_PIX_SECRET": secrets.token_hex(16),
            "CELERY_TASK_ALWAYS_EAGER": "1",
            "CLINIC_SECRET_BACKEND": "synthetic-file",
            "CLINIC_SECRET_DIR": str(secret_dir),
            "COMMS_SYNTHETIC_CHANNELS": "email",
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "EHR_ATTACHMENT_ROOT": str(attachment_root),
            "MIGRATION_DATABASE_URL": database_url(
                "clinic_owner", lease.owner_password, lease
            ),
            "PHYSICIAN_SYNTHETIC_REGISTRY": "1",
            "PRESCRIPTION_SYNTHETIC_SIGNING": "1",
            "SCRIBE_MEDIA_ROOT": str(work_dir / "media-source"),
            "TELECONSULT_SYNTHETIC_PROVIDER": "1",
        }
    )
    _run(
        (
            str(PYTHON),
            "-m",
            "ops.testing.restore_fixture",
            "seed",
            "--probe-path",
            str(work_dir / "probe.json"),
        ),
        seed_environment,
    )


def _rotate(lease: CiDatabaseLease, secret_dir: Path) -> JsonObject:
    """Rotate the source's DEK and KEK through the owner fixture step."""
    environment = _base_environment()
    environment.update(
        {
            "CLINIC_SECRET_BACKEND": "synthetic-file",
            "CLINIC_SECRET_DIR": str(secret_dir),
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "MIGRATION_DATABASE_URL": database_url(
                "clinic_owner", lease.owner_password, lease
            ),
        }
    )
    raw = _run(
        (str(PYTHON), "-m", "ops.testing.restore_fixture", "rotate"), environment
    )
    lines = raw.strip().splitlines()
    value: object = json.loads(lines[-1]) if lines else None
    if not isinstance(value, dict) or set(value) != {
        "dek_versions",
        "rewrapped_versions",
    }:
        _fail("restore acceptance key rotation report is invalid")
    return {str(key): item for key, item in value.items()}


def _escrow(root: Path, secret_dir: Path) -> Path:
    """Escrow the source's current KEK beside, never inside, the backup."""
    root.mkdir(mode=0o700)
    _write_kek(root, (secret_dir / KEK_SECRET_FILE).read_text(encoding="ascii"))
    return root


def _write_kek(root: Path, value: str) -> None:
    descriptor = os.open(
        root / KEK_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400
    )
    try:
        os.write(descriptor, value.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _failure_variants(
    source: CiDatabaseLease,
    target: CiDatabaseLease,
    work_dir: Path,
    inputs: RestoreInputs,
    stale_kek: str,
) -> list[JsonValue]:
    """Prove each missing or stale recovery input refuses before any mutation.

    The full restore that follows starts with the empty-target gate, so it also
    proves none of these refusals touched the target.
    """
    missing_key = work_dir / "escrow-missing"
    missing_key.mkdir(mode=0o700)
    stale_key = work_dir / "escrow-stale"
    stale_key.mkdir(mode=0o700)
    _write_kek(stale_key, stale_kek)
    incomplete = work_dir / "object-backup-incomplete"
    shutil.copytree(inputs.object_backup, incomplete)
    objects = sorted(
        path for path in incomplete.rglob("*") if path.is_file() and path.suffix == ""
    )
    if not objects:
        _fail("restore acceptance object backup is empty")
    objects[0].unlink()
    results: list[JsonValue] = []
    for variant, variant_inputs, expected in (
        ("missing-key", replace(inputs, key_escrow=missing_key), MISSING_ESCROW),
        ("stale-key", replace(inputs, key_escrow=stale_key), STALE_ESCROW),
        ("missing-object", replace(inputs, object_backup=incomplete), MISSING_OBJECT),
    ):
        code, output = _attempt_restore(source, target, work_dir, variant_inputs)
        message = next(
            (
                line.split("RestoreContractError: ", 1)[1]
                for line in reversed(output.splitlines())
                if "RestoreContractError: restore drill failed: " in line
            ),
            "",
        )
        if code == 0 or expected not in message:
            _fail(f"restore drill variant {variant} was not refused honestly")
        results.append(
            {"exit_code": code, "message": message, "refused": True, "variant": variant}
        )
    return results


def _run_restore(
    source: CiDatabaseLease,
    target: CiDatabaseLease,
    work_dir: Path,
    inputs: RestoreInputs,
) -> bytes:
    """Run the full rehearsal and return its published evidence bytes."""
    evidence = work_dir / "restore-rehearsal.json"
    code, output = _attempt_restore(source, target, work_dir, inputs, evidence)
    if code != 0:
        _fail(f"restore acceptance command failed: {output[-4000:]}")
    raw = evidence.read_bytes()
    evidence.chmod(0o600)
    evidence.unlink()
    return raw


def _attempt_restore(
    source: CiDatabaseLease,
    target: CiDatabaseLease,
    work_dir: Path,
    inputs: RestoreInputs,
    evidence: Path | None = None,
) -> tuple[int, str]:
    """Invoke ``make restore-rehearsal`` once; return its exit code and output."""
    evidence = evidence or work_dir / "refused-rehearsal.json"
    media_store = work_dir / "media-restored"
    media_store.mkdir(mode=0o700, exist_ok=True)
    uv_cache = work_dir / "uv-cache"
    uv_cache.mkdir(mode=0o700, exist_ok=True)
    credentials = {
        "source_password": source.super_password,
        "target_app_password": target.app_password,
        "target_app_url": database_url("clinic_app", target.app_password, target),
        "target_password": target.super_password,
    }
    read_descriptor, write_descriptor = os.pipe()
    try:
        os.write(
            write_descriptor,
            json.dumps(credentials, separators=(",", ":"), sort_keys=True).encode()
            + b"\n",
        )
        os.close(write_descriptor)
        write_descriptor = -1
        environment = _base_environment()
        environment.update(
            {
                "RESTORE_ATTACHMENT_ROOT": str(inputs.object_store),
                "RESTORE_CREDENTIALS_FD": str(read_descriptor),
                "RESTORE_EVIDENCE_PATH": str(evidence),
                "RESTORE_KEY_ESCROW": str(inputs.key_escrow),
                "RESTORE_MEDIA_STORE": str(media_store),
                "RESTORE_OBJECT_BACKUP": str(inputs.object_backup),
                "RESTORE_OBJECT_STORE": str(inputs.object_store),
                "RESTORE_PROBE_PATH": str(inputs.probe),
                "RESTORE_SOURCE_CLAIM": source.claim_id,
                "RESTORE_SOURCE_CONTAINER": source.container_id,
                "RESTORE_SOURCE_DATABASE": source.database,
                "RESTORE_SOURCE_MEDIA_STORE": str(work_dir / "media-source"),
                "RESTORE_TARGET_CLAIM": target.claim_id,
                "RESTORE_TARGET_CONTAINER": target.container_id,
                "RESTORE_TARGET_DATABASE": target.database,
                "RESTORE_WORK_DIR": str(work_dir),
                "UV_CACHE_DIR": str(uv_cache),
                "UV_PROJECT_ENVIRONMENT": str(PYTHON.parents[1]),
            }
        )
        return _attempt(
            ("/usr/bin/make", f"UV={UV}", "restore-rehearsal"),
            environment,
            (read_descriptor,),
        )
    finally:
        os.close(read_descriptor)
        if write_descriptor >= 0:
            os.close(write_descriptor)


def _source_revision() -> JsonObject:
    """Bind the report to the exact source revision that ran it."""
    environment = _base_environment()
    commit = _run((str(GIT), "rev-parse", "HEAD"), environment).strip()
    tree = _run((str(GIT), "rev-parse", "HEAD^{tree}"), environment).strip()
    status = _run((str(GIT), "status", "--porcelain"), environment)
    return {"clean": not status.strip(), "commit": commit, "tree": tree}


def _base_environment() -> dict[str, str]:
    return {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
    }


def _run(
    argv: tuple[str, ...],
    environment: dict[str, str],
    pass_fds: tuple[int, ...] = (),
) -> str:
    code, output, stdout = asyncio.run(_run_async(argv, environment, pass_fds))
    if code != 0:
        _fail(f"restore acceptance command failed: {output[-4000:]}")
    return stdout


def _attempt(
    argv: tuple[str, ...],
    environment: dict[str, str],
    pass_fds: tuple[int, ...] = (),
) -> tuple[int, str]:
    code, output, _stdout = asyncio.run(_run_async(argv, environment, pass_fds))
    return code, output


async def _run_async(
    argv: tuple[str, ...],
    environment: dict[str, str],
    pass_fds: tuple[int, ...],
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=PROJECT_ROOT,
        env=environment,
        pass_fds=pass_fds,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    code = process.returncode if process.returncode is not None else 1
    return (
        code,
        (stdout + stderr).decode("utf-8", errors="replace"),
        stdout.decode("utf-8", errors="replace"),
    )


def _fail(reason: str) -> Never:
    raise RuntimeError(reason)


if __name__ == "__main__":
    raise SystemExit(main())
