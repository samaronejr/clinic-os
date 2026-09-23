"""Supervise the real renewal browser route against task-owned PostgreSQL.

``browser --suite <name>`` captures the current worktree source identity
through the task-3 snapshot contract, provisions a unique claimed PostgreSQL
container, migrates and seeds it through the owner role, serves the product
through a supervised Gunicorn master bound to loopback as ``clinic_app`` with
the real middleware/CSRF/RLS stack, and drives the registered pytest browser
suite through a real Chromium executable. ``ci`` runs the static, migration,
coverage, dependency, current-source image/TLS, and browser gates in order.

Only resources created by this runner are removed; the artifact root is
retained as evidence.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Never, cast
from urllib.parse import quote, urlsplit
from uuid import uuid4

import psycopg

from ops.testing.browser_server_controller import reserve_port, wait_until_ready
from ops.testing.browser_server_supervisor import (
    SupervisedMaster,
    start_master,
    supervised_argv,
)
from ops.testing.current_source_snapshot import (
    capture_current_source,
    verify_current_source_record,
)
from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    ensure_private_directory,
    utc_now,
    write_no_replace,
)
from ops.testing.runtime_paths import runtime_directory

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

SUITES: Final = {
    "clinic-settings": ("tests/renewal/browser/test_clinic_settings.py",),
    "billing": ("tests/renewal/browser/test_billing.py",),
    "prescription-draft": ("tests/renewal/browser/test_prescription_draft.py",),
    "prescribing": ("tests/renewal/browser/test_prescribing.py",),
    "document-verification": ("tests/renewal/browser/test_document_verification.py",),
    "consent": ("tests/renewal/browser/test_consent.py",),
    "amendments": ("tests/renewal/browser/test_amendments.py",),
    "attachments": ("tests/renewal/browser/test_attachments.py",),
    "clinical-history": ("tests/renewal/browser/test_clinical_history.py",),
    "clinician-video": ("tests/renewal/browser/test_clinician_video.py",),
    "video-recovery": ("tests/renewal/browser/test_video_recovery.py",),
    "encounter": ("tests/renewal/browser/test_encounter.py",),
    "end-to-end": ("tests/renewal/browser/test_end_to_end.py",),
    "agenda": ("tests/renewal/browser/test_agenda.py",),
    "availability": ("tests/renewal/browser/test_availability.py",),
    "contacts": ("tests/renewal/browser/test_contacts.py",),
    "locale": ("tests/renewal/browser/test_locale.py",),
    "patient-access": ("tests/renewal/browser/test_patient_access.py",),
    "patient-video": ("tests/renewal/browser/test_patient_video.py",),
    "questionnaires": ("tests/renewal/browser/test_questionnaires.py",),
    "self-booking": ("tests/renewal/browser/test_self_booking.py",),
    "waitlist": ("tests/renewal/browser/test_waitlist.py",),
    "reminders": ("tests/renewal/browser/test_reminders.py",),
    "teleconsult": ("tests/renewal/browser/test_teleconsult.py",),
    "retention": ("tests/renewal/browser/test_retention.py",),
    "primitives": ("tests/renewal/browser/test_primitives.py",),
    "smoke": ("tests/renewal/browser/test_smoke.py",),
    "staff-intake": ("tests/renewal/browser/test_staff_intake.py",),
    "workspace": ("tests/renewal/browser/test_workspace.py",),
}
# Suites whose fixtures seed synthetic staff through the owner DSN.
FIXTURE_SUITES: Final = frozenset(
    {
        "agenda",
        "clinic-settings",
        "billing",
        "prescription-draft",
        "prescribing",
        "document-verification",
        "amendments",
        "attachments",
        "encounter",
        "end-to-end",
        "clinical-history",
        "clinician-video",
        "video-recovery",
        "availability",
        "contacts",
        "consent",
        "locale",
        "patient-access",
        "patient-video",
        "questionnaires",
        "self-booking",
        "waitlist",
        "reminders",
        "teleconsult",
        "retention",
        "staff-intake",
        "workspace",
    }
)
# Suites that exercise the synthetic room provider through the real outbox.
VIDEO_SUITES: Final = frozenset(
    {
        "teleconsult",
        "patient-video",
        "clinician-video",
        "video-recovery",
        "end-to-end",
    }
)
# Suites that drive the synthetic signing provider and physician registry.
SIGNING_SUITES: Final = frozenset(
    {"document-verification", "prescribing", "end-to-end"}
)
# Suites that exercise the synthetic, explicitly non-payable PIX rehearsal.
PAYMENT_SUITES: Final = frozenset({"billing", "end-to-end"})
CI_GATES: Final = (
    "static",
    "migration",
    "coverage",
    "dependency",
    "image-tls",
    "browser",
)
COVERAGE_TARGETS: Final = (
    "--cov=apps.audit",
    "--cov=apps.billing",
    "--cov=apps.comms",
    "--cov=apps.consent",
    "--cov=apps.ehr",
    "--cov=apps.prescription",
    "--cov=apps.core",
    "--cov=apps.identity",
    "--cov=apps.intake",
    "--cov=apps.interop",
    "--cov=apps.retention",
    "--cov=apps.scheduling",
    "--cov=apps.teleconsult",
    "--cov=apps.tenancy",
)
POSTGRES_IMAGE: Final = "postgres:16"
POSTGRES_CONTAINER_PORT: Final = 5432
PROTECTED_DATABASE_PORT: Final = 5432
APP_ROLE: Final = "clinic_app"
OWNER_ROLE: Final = "clinic_owner"
SUPER_ROLE: Final = "clinic_super"
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
BROWSER_CANDIDATES: Final = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
EXCLUDED_COMPONENTS: Final = frozenset(
    {
        ".agents",
        ".claude",
        ".codex",
        ".git",
        ".omo",
        ".omx",
        ".venv",
        "__pycache__",
        "evidence",
        "node_modules",
        "venv",
    }
)
EXCLUDED_NAMES: Final = frozenset(
    {
        ".env",
        ".env.local",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
)
EXCLUDED_SUFFIXES: Final = (".pem", ".key", ".p12", ".pfx")
DATABASE_READY_TIMEOUT_SECONDS: Final = 90
DATABASE_POLL_SECONDS: Final = 0.5
MIGRATION_TIMEOUT_SECONDS: Final = 600
PROVISION_TIMEOUT_SECONDS: Final = 300
PYTEST_TIMEOUT_SECONDS: Final = 900
COMMAND_TIMEOUT_SECONDS: Final = 60
STATIC_GATE_TIMEOUT_SECONDS: Final = 900
COVERAGE_TIMEOUT_SECONDS: Final = 3600
DEPENDENCY_TIMEOUT_SECONDS: Final = 900
IMAGE_TLS_TIMEOUT_SECONDS: Final = 3600
BUILD_TIMEOUT_SECONDS: Final = 3600
MAX_LOG_TAIL_BYTES: Final = 4000
KEK_SECRET_FILE: Final = "tenant-kek.secret"  # noqa: S105 - a file name
_BROWSER_OPTIONS: Final = frozenset(
    {"--artifact-root", "--record", "--run-root", "--suite"}
)
_CI_OPTIONS: Final = frozenset({"--artifact-root", "--run-root"})


class RenewalRunnerError(IsolationError):
    """Reject a malformed or unprovable renewal-runner invocation."""


class RenewalInterruptError(RenewalRunnerError):
    """Abort the run while still unwinding every owned resource."""


def _fail(message: str) -> Never:
    raise RenewalRunnerError(message)


@dataclass(frozen=True, slots=True)
class ProvisionedDatabase:
    """Endpoint and credential values for one runner-created database."""

    app_dsn: str
    app_password: str
    container: str
    database: str
    owner_dsn: str
    owner_password: str
    port: int
    postgres_password: str
    super_dsn: str
    super_password: str
    volume: str


def _excluded(path: str) -> bool:
    """Mirror the snapshot exclusion contract for allowlist computation."""
    for part in PurePosixPath(path).parts:
        if part in EXCLUDED_COMPONENTS or part in EXCLUDED_NAMES:
            return True
        if part.startswith(".env.") and part != ".env.example":
            return True
        if part.endswith(EXCLUDED_SUFFIXES):
            return True
    return False


def _repository() -> Path:
    root = Path.cwd()
    if not root.is_absolute() or root.is_symlink():
        _fail("renewal runner must run from a canonical repository root")
    resolved = root.resolve(strict=True)
    if resolved != root:
        _fail("renewal runner repository root is noncanonical")
    git = shutil.which("git")
    if git is None:
        _fail("git executable is unavailable")
    completed = subprocess.run(  # noqa: S603 - fixed executable, closed argv.
        (git, "-C", str(root), "rev-parse", "--show-toplevel"),
        check=False,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        _fail("renewal runner must run inside a Git checkout")
    try:
        top = Path(completed.stdout.decode("ascii").strip()).resolve(strict=True)
    except (OSError, UnicodeDecodeError) as error:
        message = "renewal runner repository root is not canonical"
        raise RenewalRunnerError(message) from error
    if top != resolved:
        _fail("renewal runner must run from the Git checkout root")
    return resolved


def _artifact_root(raw: str | None, repository: Path) -> Path:
    """Resolve the retained artifact root; it must never poison the snapshot."""
    if raw is None:
        root = (
            repository
            / ".omo"
            / "renewal-runs"
            / f"{utc_now().replace(':', '-')}-{secrets.token_hex(4)}"
        )
    else:
        root = Path(raw)
    if not root.is_absolute() or root.is_symlink():
        _fail("renewal artifact root must be an absolute non-symlink path")
    resolved_parent = root.parent.resolve(strict=False)
    resolved = resolved_parent / root.name
    if resolved != root:
        _fail("renewal artifact root is noncanonical")
    try:
        relative = resolved.relative_to(repository)
    except ValueError:
        relative = None
    if relative is not None and not _excluded(str(PurePosixPath(*relative.parts))):
        _fail("renewal artifact root inside the repository must be excluded")
    resolved.parent.mkdir(mode=MODE_DIRECTORY, parents=True, exist_ok=True)
    ensure_private_directory(resolved)
    return resolved


def _run_root(raw: str | None, artifact_root: Path) -> Path:
    root = artifact_root / "run" if raw is None else Path(raw)
    if not root.is_absolute() or root.is_symlink():
        _fail("renewal run root must be an absolute non-symlink path")
    root.parent.mkdir(mode=MODE_DIRECTORY, parents=True, exist_ok=True)
    ensure_private_directory(root)
    return root


def _resolve_browser() -> str:
    """Return the real Chromium executable or reject the run."""
    override = os.environ.get("CLINIC_RENEWAL_BROWSER_EXECUTABLE", "")
    if override:
        candidate = Path(override)
        if (
            candidate.is_absolute()
            and not candidate.is_symlink()
            and candidate.is_file()
            and os.access(candidate, os.X_OK)
        ):
            return str(candidate)
        _fail("renewal browser executable override is not executable")
    for name in BROWSER_CANDIDATES:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    _fail("renewal browser executable is unavailable")


def _validate_serving_dsn(dsn: str) -> str:
    """Accept only a loopback PostgreSQL DSN for the exact app role."""
    try:
        parsed = urlsplit(dsn)
        port = parsed.port
    except ValueError as error:
        message = "renewal serving DSN is malformed"
        raise RenewalRunnerError(message) from error
    role = parsed.username or ""
    host = parsed.hostname or ""
    database = parsed.path.removeprefix("/")
    if (
        parsed.scheme != "postgresql"
        or not parsed.password
        or not host
        or not database
        or "/" in database
        or parsed.fragment
        or port is None
    ):
        _fail("renewal serving DSN is malformed")
    if port == PROTECTED_DATABASE_PORT:
        _fail("renewal serving DSN targets the protected database port")
    if host not in LOOPBACK_HOSTS:
        _fail("renewal serving DSN must target the loopback host")
    if role != APP_ROLE:
        _fail("renewal serving DSN must use the clinic_app role")
    return dsn


def _require_app_role(dsn: str) -> None:
    """Prove the serving DSN authenticates as a non-superuser app role."""
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            row = connection.execute(
                "SELECT current_user, "
                "(SELECT rolsuper OR rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user)"
            ).fetchone()
    except (OSError, psycopg.Error) as error:
        message = "renewal serving DSN cannot authenticate"
        raise RenewalRunnerError(message) from error
    if row != (APP_ROLE, False):
        _fail("renewal serving DSN does not resolve to the clinic_app role")


def _docker(
    *arguments: str,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
    stdin: bytes | None = None,
) -> str:
    executable = shutil.which("docker")
    if executable is None:
        _fail("docker executable is unavailable")
    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable, closed argv.
            (executable, *arguments),
            check=False,
            capture_output=True,
            input=stdin,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        message = "renewal docker command timed out"
        raise RenewalRunnerError(message) from error
    if completed.returncode != 0:
        tail = completed.stderr.decode("utf-8", errors="replace")[-500:]
        _fail(f"renewal docker command failed: {tail.strip()}")
    return completed.stdout.decode("utf-8", errors="replace")


def _unused_loopback_port() -> int:
    for _attempt in range(32):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        if port != PROTECTED_DATABASE_PORT:
            return port
    _fail("no free loopback port")


def _dsn(role: str, password: str, port: int, database: str) -> str:
    encoded = quote(password, safe="")
    return f"postgresql://{role}:{encoded}@127.0.0.1:{port}/{database}"


@contextmanager
def _provision_database(
    repository: Path,
    token: str,
    *,
    docker: Callable[..., str] = _docker,
) -> Iterator[ProvisionedDatabase]:
    """Create one unique PostgreSQL container and remove only what it made."""
    container = f"clinic_renewal_db_{token}"
    volume = f"clinic_renewal_db_{token}_data"
    database = "clinic"
    port = _unused_loopback_port()
    passwords = {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "CLINIC_OWNER_PASSWORD": secrets.token_urlsafe(24),
        "CLINIC_APP_PASSWORD": secrets.token_urlsafe(24),
        "CLINIC_SUPER_PASSWORD": secrets.token_urlsafe(24),
    }
    bootstrap = repository / "ops/db/bootstrap.sql"
    init = repository / "ops/db/init"
    if not bootstrap.is_file() or not init.is_dir():
        _fail("renewal database bootstrap inputs are missing")
    docker("volume", "create", "--label", f"clinic.renewal.run={token}", volume)
    try:
        docker(
            "run",
            "--detach",
            "--name",
            container,
            "--label",
            f"clinic.renewal.run={token}",
            "--publish",
            f"127.0.0.1:{port}:5432/tcp",
            "--env",
            f"POSTGRES_DB={database}",
            "--env",
            "POSTGRES_USER=postgres",
            *(
                argument
                for name, value in sorted(passwords.items())
                for argument in ("--env", f"{name}={value}")
            ),
            "--mount",
            f"type=volume,src={volume},dst=/var/lib/postgresql/data",
            "--mount",
            f"type=bind,src={bootstrap},dst=/opt/clinic/bootstrap.sql,readonly",
            "--mount",
            f"type=bind,src={init},dst=/docker-entrypoint-initdb.d,readonly",
            POSTGRES_IMAGE,
        )
        _await_database(container, docker=docker)
        yield ProvisionedDatabase(
            _dsn(APP_ROLE, passwords["CLINIC_APP_PASSWORD"], port, database),
            passwords["CLINIC_APP_PASSWORD"],
            container,
            database,
            _dsn(OWNER_ROLE, passwords["CLINIC_OWNER_PASSWORD"], port, database),
            passwords["CLINIC_OWNER_PASSWORD"],
            port,
            passwords["POSTGRES_PASSWORD"],
            _dsn(SUPER_ROLE, passwords["CLINIC_SUPER_PASSWORD"], port, database),
            passwords["CLINIC_SUPER_PASSWORD"],
            volume,
        )
    finally:
        _remove_owned_database(container, volume, docker)


def _remove_owned_database(
    container: str,
    volume: str,
    docker: Callable[..., str],
) -> None:
    """Remove both owned resources; never turn failed teardown into success.

    Every removal is attempted even when an earlier one fails or the run is
    interrupted again mid-teardown; resources the daemon reports as absent
    are already removed. Failures propagate with the owned-resource identity
    so recovery can find them, and an in-flight or fresh interruption always
    wins the exit contract.
    """
    pending = sys.exception()
    failures: list[str] = []
    interrupted: RenewalInterruptError | None = None
    for arguments in (
        ("container", "rm", "-f", container),
        ("volume", "rm", volume),
    ):
        try:
            docker(*arguments)
        except RenewalInterruptError as error:
            interrupted = error
        except RenewalRunnerError as error:
            if "no such" not in str(error).lower():
                failures.append(f"{' '.join(arguments)}: {error}")
    detail = "; ".join(failures)
    if interrupted is not None or isinstance(pending, RenewalInterruptError):
        message = "renewal runner interrupted"
        if detail:
            message = f"{message}; teardown incomplete: {detail}"
        raise RenewalInterruptError(message) from interrupted or pending
    if failures:
        _fail(f"renewal teardown failed; owned resources retained: {detail}")


def _await_database(
    container: str,
    *,
    docker: Callable[..., str],
) -> None:
    deadline = time.monotonic() + DATABASE_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            docker(
                "exec",
                container,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
            )
        except RenewalInterruptError:
            raise
        except RenewalRunnerError:
            time.sleep(DATABASE_POLL_SECONDS)
        else:
            return
    _fail("renewal PostgreSQL never became ready")


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    environment = {
        "HOME": os.environ.get("HOME", "/"),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(_repository()),
        "PYTHONTZPATH": "",
    }
    environment.update(extra)
    return environment


def _run_bounded(
    argv: list[str],
    environment: dict[str, str],
    log: Path,
    *,
    timeout: int,
    cwd: Path | None = None,
) -> int:
    """Run one child in its own process group with a hard wall-clock bound."""
    with log.open("wb") as stream:
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter, closed argv.
            argv,
            cwd=cwd,
            env=environment,
            start_new_session=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=COMMAND_TIMEOUT_SECONDS)
            return 124
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=COMMAND_TIMEOUT_SECONDS)
            raise


def _migrate(repository: Path, owner_dsn: str, log: Path) -> None:
    code = _run_bounded(
        [sys.executable, "manage.py", "migrate", "--no-input"],
        _child_env(
            {
                "APP_DATABASE_URL": owner_dsn,
                "CLINIC_DATA_MODE": "synthetic",
                "DJANGO_SETTINGS_MODULE": "config.settings.base",
            }
        ),
        log,
        timeout=MIGRATION_TIMEOUT_SECONDS,
        cwd=repository,
    )
    if code != 0:
        _fail(f"renewal owner-role migrations failed (exit {code})")


def _seed(
    repository: Path,
    owner_dsn: str,
    log: Path,
    secret_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Provision the smoke fixture through the owner bootstrap service.

    With a secret store, the organization also receives its tenant DEK so
    protected fields can be written; without one, protected writes fail
    closed exactly as the envelope contract requires.
    """
    organization_id = str(uuid4())
    clinic_id = str(uuid4())
    owner_id = str(uuid4())
    username = f"renewal-owner-{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(18)
    code = _run_bounded(
        [sys.executable, "-m", "ops.testing.renewal_provision"],
        _child_env(
            {
                "APP_DATABASE_URL": owner_dsn,
                "CLINIC_DATA_MODE": "synthetic",
                "DJANGO_SETTINGS_MODULE": "config.settings.base",
                "RENEWAL_CLINIC_ID": clinic_id,
                "RENEWAL_CLINIC_NAME": "Renewal Smoke Clinic",
                "RENEWAL_CNPJ": "11222333000181",
                "RENEWAL_CRM_UF": "SP",
                "RENEWAL_ORGANIZATION_ID": organization_id,
                "RENEWAL_ORGANIZATION_NAME": "Renewal Smoke Organization",
                "RENEWAL_OWNER_EMAIL": f"{username}@renewal.invalid",
                "RENEWAL_OWNER_ID": owner_id,
                "RENEWAL_OWNER_PASSWORD": password,
                "RENEWAL_OWNER_USERNAME": username,
                "RENEWAL_TIMEZONE": "America/Sao_Paulo",
                **(secret_environment or {}),
            }
        ),
        log,
        timeout=PROVISION_TIMEOUT_SECONDS,
        cwd=repository,
    )
    if code != 0:
        _fail(f"renewal owner-role provisioning failed (exit {code})")
    return {
        "clinic_id": clinic_id,
        "organization_id": organization_id,
        "owner_id": owner_id,
        "password": password,
        "username": username,
    }


@contextmanager
def _synthetic_secret_store(run_root: Path) -> Iterator[dict[str, str]]:
    """Mint one disposable synthetic tenant KEK for this run's runtime.

    Protected fields fail closed without a configured secret store
    (``apps/core/secrets.py``). The ``synthetic-file`` backend is the
    rehearsal stand-in for the approved managed store; its material lives in
    a private runtime child of the run root and is removed with it.
    """
    with runtime_directory(run_root, purpose="renewal-secrets") as root:
        write_no_replace(
            root / KEK_SECRET_FILE,
            secrets.token_hex(32).encode("ascii"),
            mode=MODE_PRIVATE,
        )
        yield {
            "CLINIC_SECRET_BACKEND": "synthetic-file",
            "CLINIC_SECRET_DIR": str(root),
        }


def _synthetic_adapters(suite: str) -> dict[str, str]:
    """Opt one suite's runtime into exactly the synthetic adapters it drives."""
    return {
        **(
            {"COMMS_SYNTHETIC_CHANNELS": "email,sms,whatsapp"}
            if suite == "reminders"
            else {}
        ),
        **({"TELECONSULT_SYNTHETIC_PROVIDER": "true"} if suite in VIDEO_SUITES else {}),
        **(
            {
                "PRESCRIPTION_SYNTHETIC_SIGNING": "true",
                "PHYSICIAN_SYNTHETIC_REGISTRY": "true",
                "COMMS_SYNTHETIC_CHANNELS": "email",
            }
            if suite in SIGNING_SUITES
            else {}
        ),
        **({"BILLING_SYNTHETIC_PIX": "true"} if suite in PAYMENT_SUITES else {}),
    }


def _server_environment(app_dsn: str) -> dict[str, str]:
    return _child_env(
        {
            "ALLOWED_HOSTS": "127.0.0.1,localhost",
            "APP_DATABASE_URL": app_dsn,
            "CLINIC_DATA_MODE": "synthetic",
            "DJANGO_SETTINGS_MODULE": "config.settings.renewal",
            "SECRET_KEY": secrets.token_urlsafe(48),
        }
    )


def _pytest_environment(
    *,
    base_url: str,
    artifact_root: Path,
    browser: str,
    username: str,
    password: str,
) -> dict[str, str]:
    """Export only the private runner-to-fixture inputs to the suite child."""
    return _child_env(
        {
            "CLINIC_RENEWAL_ARTIFACT_ROOT": str(artifact_root),
            "CLINIC_RENEWAL_BASE_URL": base_url,
            "CLINIC_RENEWAL_BROWSER_EXECUTABLE": browser,
            "CLINIC_RENEWAL_PASSWORD": password,
            "CLINIC_RENEWAL_USERNAME": username,
        }
    )


def _junit_verdict(path: Path) -> tuple[int, int, int, int]:
    """Return (tests, failures, errors, skipped) from the suite junit report."""
    if not path.is_file() or path.is_symlink():
        _fail("renewal suite produced no junit report")
    try:
        document = ET.parse(path)  # noqa: S314 - runner-owned file.
    except ET.ParseError as error:
        message = "renewal suite junit report is malformed"
        raise RenewalRunnerError(message) from error
    root = document.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        _fail("renewal suite junit report has no testsuite")
    counts = [0, 0, 0, 0]
    for suite in suites:
        for index, name in enumerate(("tests", "failures", "errors", "skipped")):
            raw = suite.get(name, "")
            if not raw.isdecimal():
                _fail("renewal suite junit report is malformed")
            counts[index] += int(raw)
    return counts[0], counts[1], counts[2], counts[3]


def _capture_source(
    repository: Path,
    run_root: Path,
    record: Path | None,
) -> tuple[JsonObject, str]:
    """Bind this run to the current worktree bytes through the snapshot contract."""
    if record is not None:
        verified = verify_current_source_record(repository, record, run_root)
        manifest = verified["snapshot_manifest"]
        if not isinstance(manifest, dict):
            _fail("renewal source record is malformed")
        digest = verified["snapshot_manifest_sha256"]
        if not isinstance(digest, str):
            _fail("renewal source record is malformed")
        return manifest, digest
    authorized = _authorized_untracked(repository)
    with runtime_directory(run_root, purpose="renewal-source") as staging:
        manifest, digest = capture_current_source(
            repository,
            staging,
            authorized_untracked=authorized,
        )
    return manifest, digest


def _authorized_untracked(repository: Path) -> tuple[str, ...]:
    git = shutil.which("git")
    if git is None:
        _fail("git executable is unavailable")
    completed = subprocess.run(  # noqa: S603 - fixed executable, closed argv.
        (
            git,
            "-C",
            str(repository),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ),
        check=False,
        capture_output=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        _fail("renewal untracked enumeration failed")
    paths = completed.stdout.decode("utf-8").split("\0")
    return tuple(sorted(path for path in paths if path and not _excluded(path)))


def _run_browser_suite(
    repository: Path,
    suite: str,
    artifact_root: Path,
    run_root: Path,
    record: Path | None,
) -> JsonObject:
    """Execute one registered suite against the real supervised runtime."""
    browser = _resolve_browser()
    override = os.environ.get("CLINIC_RENEWAL_APP_DATABASE_URL", "")
    override_dsn = _validate_serving_dsn(override) if override else None
    manifest, digest = _capture_source(repository, run_root, record)
    browser_root = artifact_root / "browser"
    ensure_private_directory(browser_root)
    junit = browser_root / f"junit-{suite}.xml"
    pytest_log = artifact_root / "pytest.log"
    server_log = artifact_root / "server.log"
    access_log = artifact_root / "access.log"
    token = secrets.token_hex(8)
    provisioned: ProvisionedDatabase | None = None
    fixture: dict[str, str] = {"password": "", "username": ""}
    database_cm = (
        contextlib.nullcontext() if override else _provision_database(repository, token)
    )
    with (
        _synthetic_secret_store(run_root) as secret_environment,
        database_cm as database,
    ):
        if override_dsn is not None:
            app_dsn = override_dsn
        else:
            if not isinstance(database, ProvisionedDatabase):
                _fail("renewal database provisioning returned no lease")
            provisioned = database
            _migrate(repository, provisioned.owner_dsn, pytest_log)
            fixture = _seed(
                repository, provisioned.owner_dsn, pytest_log, secret_environment
            )
            app_dsn = _validate_serving_dsn(provisioned.app_dsn)
        _require_app_role(app_dsn)
        port = reserve_port()
        base_url = f"http://127.0.0.1:{port}"
        argv = supervised_argv(Path(sys.executable), port, access_log)
        spawned: list[SupervisedMaster] = []
        try:
            start_master(
                argv,
                {
                    **_server_environment(app_dsn),
                    **secret_environment,
                    **_synthetic_adapters(suite),
                },
                server_log,
                owner=spawned,
            )
            wait_until_ready(port, server_log)
            # Workers spawned by the suite read protected fields too.
            suite_environment = {
                **_pytest_environment(
                    base_url=base_url,
                    artifact_root=browser_root,
                    browser=browser,
                    username=fixture["username"],
                    password=fixture["password"],
                ),
                **secret_environment,
            }
            # Owner access is fixture-only; Gunicorn retains clinic_app.
            suite_environment.update(
                {
                    "CLINIC_RENEWAL_FIXTURE_DATABASE_URL": provisioned.owner_dsn,
                    "CLINIC_RENEWAL_CLINIC_ID": fixture["clinic_id"],
                    "CLINIC_RENEWAL_ORGANIZATION_ID": fixture["organization_id"],
                    **(
                        {"CLINIC_RENEWAL_WORKER_DATABASE_URL": app_dsn}
                        if suite == "reminders"
                        or suite in VIDEO_SUITES
                        or suite == "document-verification"
                        else {}
                    ),
                }
                if suite in FIXTURE_SUITES and provisioned is not None
                else {}
            )
            pytest_code = _run_bounded(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={junit}",
                    *SUITES[suite],
                ],
                suite_environment,
                pytest_log,
                timeout=PYTEST_TIMEOUT_SECONDS,
                cwd=repository,
            )
        finally:
            if spawned:
                _terminate_master(spawned[0])
    if not junit.is_file():
        _fail(f"renewal suite produced no junit report (pytest exit {pytest_code})")
    tests, failures, errors, skipped = _junit_verdict(junit)
    if tests == 0:
        _fail("renewal suite ran zero tests")
    if skipped:
        _fail("renewal suite skipped tests; the run proves nothing")
    if pytest_code != 0 or failures or errors:
        _fail(f"renewal suite failed (pytest exit {pytest_code})")
    entries = manifest.get("entries")
    return {
        "artifact_root": str(artifact_root),
        "base_url": base_url,
        "browser": browser,
        "pytest_exit": pytest_code,
        "runtime_role": APP_ROLE,
        "schema_version": 1,
        "source_entry_count": len(entries) if isinstance(entries, list) else 0,
        "source_manifest_sha256": digest,
        "suite": suite,
        "tests": tests,
        "verified_record": str(record) if record is not None else None,
    }


def _gate_static(repository: Path, logs: Path) -> list[JsonObject]:
    venv = repository / ".venv" / "bin"
    results: list[JsonObject] = []
    for name, argv in (
        ("ruff-check", [str(venv / "ruff"), "check", "."]),
        ("ruff-format", [str(venv / "ruff"), "format", "--check", "."]),
        ("mypy", [str(venv / "mypy"), "."]),
    ):
        code = _run_bounded(
            argv,
            _child_env({}),
            logs / f"gate-static-{name}.log",
            timeout=STATIC_GATE_TIMEOUT_SECONDS,
            cwd=repository,
        )
        results.append({"command": name, "exit": code})
    return results


def _terminate_master(master: SupervisedMaster) -> None:
    """Terminate the supervised group; propagate any observed interruption.

    The bounded TERM/KILL/reap is retried once so a second signal cannot
    abandon cleanup, but a cancellation observed during teardown is never
    swallowed: it is re-raised even when the retry succeeds.
    """
    interrupted: RenewalInterruptError | None = None
    for _attempt in range(2):
        try:
            master.terminate()
        except RenewalInterruptError as error:
            if interrupted is None:
                interrupted = error
        else:
            break
    if interrupted is not None:
        raise interrupted


def _gate_migration(
    repository: Path,
    logs: Path,
    token: str,
) -> list[JsonObject]:
    """Check migration drift against the task-owned database, not localhost."""
    with _provision_database(repository, f"mig{token}") as database:
        code = _run_bounded(
            [
                sys.executable,
                "manage.py",
                "makemigrations",
                "--check",
                "--dry-run",
            ],
            _child_env(
                {
                    "DJANGO_SETTINGS_MODULE": "config.settings.test",
                    "MIGRATION_DATABASE_URL": database.owner_dsn,
                }
            ),
            logs / "gate-migration.log",
            timeout=MIGRATION_TIMEOUT_SECONDS,
            cwd=repository,
        )
    return [{"command": "makemigrations-check", "exit": code}]


def _create_test_database(repository: Path, database: ProvisionedDatabase) -> None:
    """Mirror ``make db-bootstrap``: owner-owned test DB plus role bootstrap."""
    test_name = f"test_{database.database}"
    _docker(
        "exec",
        "-i",
        "-e",
        f"PGPASSWORD={database.postgres_password}",
        database.container,
        "psql",
        "-h",
        "127.0.0.1",
        "-U",
        "postgres",
        "-d",
        database.database,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        f"CREATE DATABASE {test_name} OWNER {OWNER_ROLE}",
    )
    bootstrap = (repository / "ops/db/bootstrap.sql").read_bytes()
    _docker(
        "exec",
        "-i",
        "-e",
        f"PGPASSWORD={database.postgres_password}",
        "-e",
        f"CLINIC_OWNER_PASSWORD={database.owner_password}",
        "-e",
        f"CLINIC_APP_PASSWORD={database.app_password}",
        "-e",
        f"CLINIC_SUPER_PASSWORD={database.super_password}",
        database.container,
        "psql",
        "-h",
        "127.0.0.1",
        "-U",
        "postgres",
        "-d",
        test_name,
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"database_name={test_name}",
        "-v",
        "app_schema=clinic_app",
        "-f",
        "-",
        stdin=bootstrap,
    )


def _gate_coverage(
    repository: Path,
    logs: Path,
    token: str,
) -> list[JsonObject]:
    with _provision_database(repository, f"ci{token}") as database:
        _create_test_database(repository, database)
        _migrate(repository, database.owner_dsn, logs / "gate-coverage-migrate.log")
        environment = _child_env(
            {
                "APP_DATABASE_URL": database.app_dsn,
                "DJANGO_SETTINGS_MODULE": "config.settings.test",
                "MIGRATION_DATABASE_URL": database.owner_dsn,
                "TEST_SUPERUSER_DATABASE_URL": database.super_dsn,
            }
        )
        code = _run_bounded(
            [
                sys.executable,
                "-m",
                "pytest",
                "--reuse-db",
                "-q",
                *COVERAGE_TARGETS,
                "--cov-report=term-missing",
                "--cov-fail-under=90",
                "tests",
            ],
            environment,
            logs / "gate-coverage.log",
            timeout=COVERAGE_TIMEOUT_SECONDS,
            cwd=repository,
        )
    return [{"command": "coverage", "exit": code}]


def _gate_dependency(repository: Path, logs: Path) -> list[JsonObject]:
    venv = repository / ".venv" / "bin"
    code = _run_bounded(
        [str(venv / "pip-audit"), "--local"],
        _child_env({}),
        logs / "gate-dependency.log",
        timeout=DEPENDENCY_TIMEOUT_SECONDS,
        cwd=repository,
    )
    return [{"command": "pip-audit", "exit": code}]


def _host_network_builder(context: Path, contract: JsonObject, iidfile: Path) -> None:
    """Build with ``--network=host`` for this workstation's VPN tunnel.

    The 1412-MTU route blackholes Docker bridge egress; image bytes and
    labels are identical to the stock builder.
    """
    suites = contract.get("available_suite_ids")
    if not isinstance(suites, list):
        _fail("current-source contract suites are invalid")
    docker = shutil.which("docker")
    if docker is None:
        _fail("docker executable is unavailable")
    code = _run_bounded(
        [
            docker,
            "build",
            "--network=host",
            "--platform=linux/amd64",
            "--file",
            str(context / "Dockerfile"),
            "--iidfile",
            str(iidfile),
            "--build-arg",
            "TARGETARCH=amd64",
            "--build-arg",
            f"CLINIC_REVISION_SHA={contract['revision_sha']}",
            "--build-arg",
            f"CLINIC_TREE_SHA={contract['tree_sha']}",
            "--build-arg",
            f"CLINIC_SOURCE_MANIFEST_SHA256={contract['source_manifest_sha256']}",
            "--build-arg",
            f"CLINIC_SOURCE_ENTRY_COUNT={contract['source_entry_count']}",
            "--build-arg",
            "CLINIC_AVAILABLE_SUITE_IDS=" + ",".join(str(item) for item in suites),
            str(context),
        ],
        _child_env({}),
        iidfile.parent / "docker-build.log",
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    if code != 0:
        _fail("candidate image build failed")


def _gate_image_tls(
    repository: Path,
    logs: Path,
    run_root: Path,
) -> list[JsonObject]:
    """Build the current-source pair and run the real TLS smoke gate."""
    record = logs / "current-source-record.json"
    authorized = _authorized_untracked(repository)
    if os.environ.get("CLINIC_RENEWAL_BUILD_HOST_NETWORK") == "1":
        from ops.testing.current_source_snapshot import (  # noqa: PLC0415
            CONTEXT_KINDS,
            build_current_source_record,
        )

        build_current_source_record(
            repository,
            run_root,
            record,
            authorized_untracked=authorized,
            kinds=CONTEXT_KINDS,
            builders={
                "application": _host_network_builder,
                "browser-runner": _host_network_builder,
            },
        )
        build_code = 0
    else:
        allowlist = logs / "current-source-allowlist.txt"
        allowlist.write_text("".join(f"{path}\n" for path in authorized))
        allowlist.chmod(MODE_PRIVATE)
        build_code = _run_bounded(
            [
                sys.executable,
                "-m",
                "ops.testing.current_source_snapshot",
                "build",
                "--repository",
                str(repository),
                "--run-root",
                str(run_root),
                "--report",
                str(record),
                "--allowlist",
                str(allowlist),
            ],
            _child_env({}),
            logs / "gate-image-tls-build.log",
            timeout=BUILD_TIMEOUT_SECONDS,
            cwd=repository,
        )
    results: list[JsonObject] = [
        {"command": "current-source-build", "exit": build_code}
    ]
    if build_code != 0:
        return results
    smoke_code = _run_bounded(
        [
            sys.executable,
            "-m",
            "ops.testing.https_image_controller",
            "smoke-current-source",
            "--record",
            str(record),
            "--run-root",
            str(run_root),
        ],
        _child_env({}),
        logs / "gate-image-tls-smoke.log",
        timeout=IMAGE_TLS_TIMEOUT_SECONDS,
        cwd=repository,
    )
    results.append({"command": "smoke-current-source", "exit": smoke_code})
    return results


def _gate_browser(
    repository: Path,
    artifact_root: Path,
    run_root: Path,
) -> list[JsonObject]:
    results: list[JsonObject] = []
    for suite in sorted(SUITES):
        suite_root = artifact_root / f"ci-{suite}"
        ensure_private_directory(suite_root)
        try:
            report = _run_browser_suite(repository, suite, suite_root, run_root, None)
            code = 0
        except RenewalInterruptError:
            raise
        except (IsolationError, OSError) as error:
            report = {"error": str(error)}
            code = 2
        write_no_replace(
            suite_root / "report.json",
            canonical_bytes(report),
            mode=MODE_PRIVATE,
        )
        results.append({"command": f"browser-{suite}", "exit": code})
    return results


def _run_ci(repository: Path, artifact_root: Path, run_root: Path) -> int:
    """Run every renewal CI gate in order and aggregate the exits."""
    logs = artifact_root / "gates"
    ensure_private_directory(logs)
    token = secrets.token_hex(6)
    gates: dict[str, JsonValue] = {}
    gate_functions: dict[str, Callable[[], list[JsonObject]]] = {
        "static": lambda: _gate_static(repository, logs),
        "migration": lambda: _gate_migration(repository, logs, token),
        "coverage": lambda: _gate_coverage(repository, logs, token),
        "dependency": lambda: _gate_dependency(repository, logs),
        "image-tls": lambda: _gate_image_tls(repository, logs, run_root),
        "browser": lambda: _gate_browser(repository, artifact_root, run_root),
    }
    for name in CI_GATES:
        results: list[JsonObject]
        try:
            results = gate_functions[name]()
        except RenewalInterruptError as error:
            # Cancellation is not a gate failure: record the interrupted gate,
            # keep a best-effort partial report, and stop scheduling gates.
            gates[name] = cast(
                "list[JsonValue]",
                [{"command": name, "exit": 130, "error": str(error)}],
            )
            with contextlib.suppress(IsolationError, OSError):
                _write_ci_report(artifact_root, gates)
            raise
        except (IsolationError, OSError) as error:
            results = [{"command": name, "exit": 2, "error": str(error)}]
        gates[name] = cast("list[JsonValue]", results)
    exits = _write_ci_report(artifact_root, gates)
    return 0 if exits and all(code == 0 for code in exits) else 1


def _write_ci_report(artifact_root: Path, gates: dict[str, JsonValue]) -> list[int]:
    """Persist and echo the aggregated gate report; return the exits."""
    exits = [
        int(str(item["exit"]))
        for results in gates.values()
        for item in cast("list[JsonObject]", results)
    ]
    report: JsonObject = {
        "artifact_root": str(artifact_root),
        "gates": gates,
        "ok": all(code == 0 for code in exits),
        "schema_version": 1,
    }
    write_no_replace(
        artifact_root / "ci-report.json",
        canonical_bytes(report),
        mode=MODE_PRIVATE,
    )
    sys.stdout.write(canonical_bytes(report).decode())
    return exits


def _options(arguments: list[str], allowed: frozenset[str]) -> dict[str, str]:
    options: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        name = arguments[index]
        if (
            name not in allowed
            or name in options
            or index + 1 >= len(arguments)
            or arguments[index + 1].startswith("--")
        ):
            _fail("invalid renewal-runner command grammar")
        options[name] = arguments[index + 1]
        index += 2
    return options


def _browser(arguments: list[str]) -> int:
    options = _options(arguments, _BROWSER_OPTIONS)
    suite = options.get("--suite", "")
    if suite not in SUITES:
        _fail(f"renewal browser suite is not registered: {suite or '(missing)'}")
    repository = _repository()
    artifact_root = _artifact_root(options.get("--artifact-root"), repository)
    run_root = _run_root(options.get("--run-root"), artifact_root)
    record = options.get("--record")
    report = _run_browser_suite(
        repository,
        suite,
        artifact_root,
        run_root,
        Path(record) if record else None,
    )
    write_no_replace(
        artifact_root / "report.json",
        canonical_bytes(report),
        mode=MODE_PRIVATE,
    )
    sys.stdout.write(canonical_bytes(report).decode())
    return 0


def _ci(arguments: list[str]) -> int:
    options = _options(arguments, _CI_OPTIONS)
    repository = _repository()
    artifact_root = _artifact_root(options.get("--artifact-root"), repository)
    run_root = _run_root(options.get("--run-root"), artifact_root)
    return _run_ci(repository, artifact_root, run_root)


def _interrupted(_signum: int, _frame: object) -> Never:
    message = "renewal runner interrupted"
    raise RenewalInterruptError(message)


def main(argv: list[str] | None = None) -> int:
    """Dispatch the closed browser/ci grammar; rejections exit 2."""
    arguments = sys.argv[1:] if argv is None else argv
    previous_term = signal.signal(signal.SIGTERM, _interrupted)
    previous_int = signal.signal(signal.SIGINT, _interrupted)
    try:
        if not arguments:
            _fail("invalid renewal-runner command grammar")
        operation, rest = arguments[0], arguments[1:]
        if operation == "browser":
            return _browser(rest)
        if operation == "ci":
            return _ci(rest)
        _fail("invalid renewal-runner command grammar")
    except RenewalInterruptError as error:
        sys.stderr.write(f"renewal-runner: {error}\n")
        return 130
    except (IsolationError, OSError, ValueError) as error:
        sys.stderr.write(f"renewal-runner: {error}\n")
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


if __name__ == "__main__":
    raise SystemExit(main())
