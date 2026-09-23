"""Create two claimed TLS databases and drive the disposable rehearsal."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
from pathlib import Path
from typing import Final, Never

from ops.testing.ci_postgres_runtime import (
    CiDatabaseLease,
    ci_database_lease,
    database_url,
)
from ops.testing.isolation_common import (
    ensure_private_directory,
    load_json,
)
from ops.testing.runtime_paths import runtime_directory
from ops.testing.tls_export import public_ca_export
from ops.testing.tls_materializer import materializer_lease

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
PYTHON: Final = Path(sys.executable)
UV: Final = Path(shutil.which("uv") or "")


def main(repository: Path | None = None) -> int:
    """Run target-first migration, synthetic source seed, restore, and cleanup.

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
        _seed(source, work_dir, secret_dir, attachment_source)
        attachment_restored = work_dir / "attachments-restored"
        shutil.copytree(attachment_source, attachment_restored)
        evidence = _run_restore(
            source,
            target,
            work_dir,
            work_dir / "probe.json",
            attachment_restored,
            secret_dir,
        )
        sys.stdout.buffer.write(evidence)
    return 0


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


def _run_restore(  # noqa: PLR0913 - the restore needs its full lease context
    source: CiDatabaseLease,
    target: CiDatabaseLease,
    work_dir: Path,
    probe_path: Path,
    object_store: Path,
    secret_dir: Path,
) -> bytes:
    evidence = work_dir / "restore-rehearsal.json"
    uv_cache = work_dir / "uv-cache"
    uv_cache.mkdir(mode=0o700)
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
                "RESTORE_ATTACHMENT_ROOT": str(object_store),
                "RESTORE_CREDENTIALS_FD": str(read_descriptor),
                "RESTORE_EVIDENCE_PATH": str(evidence),
                "RESTORE_OBJECT_STORE": str(object_store),
                "RESTORE_PROBE_PATH": str(probe_path),
                "RESTORE_SECRET_DIR": str(secret_dir),
                "RESTORE_SOURCE_CLAIM": source.claim_id,
                "RESTORE_SOURCE_CONTAINER": source.container_id,
                "RESTORE_SOURCE_DATABASE": source.database,
                "RESTORE_TARGET_CLAIM": target.claim_id,
                "RESTORE_TARGET_CONTAINER": target.container_id,
                "RESTORE_TARGET_DATABASE": target.database,
                "RESTORE_WORK_DIR": str(work_dir),
                "UV_CACHE_DIR": str(uv_cache),
                "UV_PROJECT_ENVIRONMENT": str(PYTHON.parents[1]),
            }
        )
        _run(
            ("/usr/bin/make", f"UV={UV}", "restore-rehearsal"),
            environment,
            (read_descriptor,),
        )
    finally:
        os.close(read_descriptor)
        if write_descriptor >= 0:
            os.close(write_descriptor)
    raw = evidence.read_bytes()
    evidence.chmod(0o600)
    evidence.unlink()
    return raw


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
) -> None:
    asyncio.run(_run_async(argv, environment, pass_fds))


async def _run_async(
    argv: tuple[str, ...],
    environment: dict[str, str],
    pass_fds: tuple[int, ...],
) -> None:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=PROJECT_ROOT,
        env=environment,
        pass_fds=pass_fds,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = (stdout + stderr).decode("utf-8", errors="replace")[-4000:]
        _fail(f"restore acceptance command failed: {detail}")


def _fail(reason: str) -> Never:
    raise RuntimeError(reason)


if __name__ == "__main__":
    raise SystemExit(main())
