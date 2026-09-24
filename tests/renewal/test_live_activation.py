"""Task-46 live-mode transition and rollback tests.

The old "unapproved live is rejected" assertions become stronger
authorized/unauthorized transition tests here: activation requires a clean
live preflight plus a separate accountable approval, the record binds the
release ID, environment, validated evidence bytes and storage, disable rolls
back without deleting anything, and every invalid setting or missing
capability fails closed — never silently back to synthetic.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

import psycopg
import pytest
from apps.core.middleware import LiveModeHaltMiddleware
from apps.ehr.attachment_storage import (
    AttachmentStorageError,
    FilesystemAttachmentStorage,
)
from config.settings.contracts import require_data_mode
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse
from django.test import override_settings
from ops.release import activation, readiness
from ops.testing.renewal_runner import _provision_database

from infra.test_production_settings import VALID_HOSTS, VALID_RUNTIME_TOKEN
from renewal.test_release_readiness import _live_bundle

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

REPOSITORY = Path(__file__).resolve().parents[2]
RELEASE_ID = "test-release-2026-09-23"
APPROVAL = "owner-signoff-2026-09-23"

_run_process = subprocess.run

_SCRUB_PREFIXES = (
    "ALLOWED_",
    "APP_",
    "BILLING_",
    "CELERY_",
    "CLINIC_",
    "COMMS_",
    "DJANGO_",
    "EHR_",
    "MIGRATION_",
    "PG",
    "PHYSICIAN_",
    "PRESCRIPTION_",
    "SECRET_",
    "SECURE_",
    "SENTRY_",
    "TELECONSULT_",
    "TEST_",
)
_SCRUB_EXACT = ("DEBUG", "FORWARDED_ALLOW_IPS", "GUNICORN_CMD_ARGS")


def _scrubbed(extra: dict[str, str]) -> dict[str, str]:
    """Return the process environment minus every clinic configuration var."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(_SCRUB_PREFIXES) and name not in _SCRUB_EXACT
    }
    environment.update(extra)
    return environment


@pytest.fixture(autouse=True)
def _clean_database_claims() -> Iterator[None]:
    """Keep the deployment-local endpoint registry test-local.

    Activation claims the bound database endpoint in
    ``activation.DATABASE_CLAIMS_DIR`` so synthetic startup can refuse it
    without the activation-state pointer. Claims persist by design, so each
    test wipes the registry before and after itself; no real activation can
    exist in this repository, so every file there is test residue.
    """
    for path in activation.DATABASE_CLAIMS_DIR.glob("*.json"):
        path.unlink()
    yield
    for path in activation.DATABASE_CLAIMS_DIR.glob("*.json"):
        path.unlink()


@pytest.fixture(autouse=True)
def _resolve_test_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the clinic-os.dev fixture hosts a deterministic address.

    The endpoint-ownership check expands every dial target through the
    system resolver, so in-process activation needs ``*.clinic-os.dev``
    to resolve without depending on public DNS: the fixture maps it to
    the TEST-NET-1 documentation address. Subprocesses keep the real
    resolver — probes that need an alias patch ``socket.getaddrinfo``
    inside the probe itself.
    """
    real_getaddrinfo = socket.getaddrinfo

    def _resolve(
        host: bytes | str | None,
        port: bytes | str | int | None = 0,
        *args: int,
        **kwargs: int,
    ) -> Sequence[tuple[object, ...]]:
        if isinstance(host, str) and host.endswith(".clinic-os.dev"):
            host = "192.0.2.40"
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


def _approved_backend_environment(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]
) -> dict[str, str]:
    """Simulate the approved managed backend for the authorization gate.

    No approved backend exists in ``SECRET_BACKENDS`` yet, so tests that
    need a real (non-rehearsal) activation record simulate one by emptying
    ``REHEARSAL_SECRET_BACKENDS`` in-process and dropping the rehearsal
    opt-in. The patch never crosses into subprocesses: production startup
    still refuses the rehearsal-only backend.
    """
    monkeypatch.setattr(activation, "REHEARSAL_SECRET_BACKENDS", frozenset())
    approved = dict(environment)
    approved[activation.LIVE_REHEARSAL_ENV] = ""
    return approved


def _live_environment(tmp_path: Path, evidence_root: Path) -> dict[str, str]:
    """Build a complete, valid rehearsal live-mode environment.

    ``synthetic-file`` is a rehearsal-only secret backend, so every
    approved-shape activation in this file runs under the explicit
    ``CLINIC_LIVE_ACTIVATION_REHEARSAL`` opt-in and is recorded as
    ``rehearsal: true``. That record exercises the transition mechanics but
    can never authorize live startup: the flag itself and the backend are
    both rejected by the production live contract. A real approval would
    use the managed store and leave the flag unset. The store must still
    return the required ``tenant-kek`` material — an empty directory is
    never readiness.
    """
    ca = tmp_path / "db-ca.pem"
    if ca.exists():
        ca.chmod(0o644)
    ca.write_text("synthetic CA fixture\n", encoding="ascii")
    ca.chmod(0o444)
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir(exist_ok=True)
    kek = secret_dir / f"{activation.REQUIRED_SECRET_NAME}.secret"
    kek.write_text("0123456789abcdef" * 4, encoding="ascii")
    kek.chmod(0o600)
    attachments = tmp_path / "live-attachments"
    attachments.mkdir(exist_ok=True)
    query = urlencode(
        {
            "connect_timeout": "2",
            "sslmode": "verify-full",
            "sslrootcert": str(ca),
        }
    )
    return {
        "ALLOWED_HOSTS": VALID_HOSTS,
        "APP_DATABASE_URL": (
            "postgresql://clinic_app:synthetic@db.qa.clinic-os.dev:5432/"
            f"clinic_live?{query}"
        ),
        "CELERY_BROKER_URL": "redis://127.0.0.1:6379/9",
        "CLINIC_DATA_MODE": "live",
        activation.ACTIVATION_APPROVAL_ENV: APPROVAL,
        activation.LIVE_REHEARSAL_ENV: "true",
        activation.ACTIVATION_STATE_ENV: str(tmp_path / "activation.json"),
        readiness.EVIDENCE_ROOT_ENV: str(evidence_root),
        readiness.RELEASE_ID_ENV: RELEASE_ID,
        "CLINIC_SECRET_BACKEND": "synthetic-file",
        "CLINIC_SECRET_DIR": str(secret_dir),
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "EHR_ATTACHMENT_ROOT": str(attachments),
        "SECRET_KEY": VALID_RUNTIME_TOKEN,
        "SECURE_SSL_HOST": "app.qa.clinic-os.dev",
    }


def _cli(
    operation: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return _run_process(
        [sys.executable, "-m", "ops.release.activation", operation],
        cwd=REPOSITORY,
        env=_scrubbed(environment),
        capture_output=True,
        text=True,
        check=False,
    )


def _settings_probe(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return _run_process(
        (
            sys.executable,
            "-c",
            "import config.settings.prod as s; print(s.CLINIC_DATA_MODE)",
        ),
        cwd=REPOSITORY,
        env=_scrubbed(environment),
        capture_output=True,
        text=True,
        check=False,
    )


def _state_path(environment: dict[str, str]) -> Path:
    return Path(environment[activation.ACTIVATION_STATE_ENV])


def _read_state(environment: dict[str, str]) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(_state_path(environment).read_text())
    return record


def test_synthetic_preflight_passes_and_reports_live_gap() -> None:
    code, report = activation._preflight_report({})
    assert code == 0
    assert report["mode"] == "synthetic"
    assert report["ready"] is True
    assert report["live_gap_report"]["ready"] is False
    assert any(
        "DJANGO_SETTINGS_MODULE" in finding
        for finding in report["live_environment_gap"]
    )
    assert report["disclaimer"]


def test_synthetic_preflight_cli_contract() -> None:
    result = _cli("preflight", {})
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["ready"] is True
    assert report["mode"] == "synthetic"


def test_live_preflight_passes_with_complete_evidence(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    code, report = activation._preflight_report(environment)
    assert code == 0
    assert report["ready"] is True
    assert report["rehearsal"] is True
    assert report["environment_findings"] == []
    assert report["missing"] == []


@pytest.mark.parametrize(
    "capability",
    [
        "dpo_designation",
        "dpo_public_contact",
        "incident_record_retention",
        "data_at_rest",
        "tenant_key_management",
        "managed_secrets",
        "tls_transport",
    ],
)
def test_live_preflight_rejects_each_missing_capability(
    tmp_path: Path, capability: str
) -> None:
    root = _live_bundle(tmp_path / "evidence")
    (root / "records" / f"{capability}.record.json").unlink()
    environment = _live_environment(tmp_path, root)
    code, report = activation._preflight_report(environment)
    assert code == 1
    assert report["ready"] is False
    assert report["missing"] == [capability]


@pytest.mark.parametrize(
    ("drift", "expected"),
    [
        ({"TELECONSULT_SYNTHETIC_PROVIDER": "true"}, "synthetic-only"),
        ({"BILLING_SYNTHETIC_PIX": "1"}, "synthetic-only"),
        ({"CELERY_TASK_ALWAYS_EAGER": "true"}, "synthetic-only"),
        ({"DEBUG": "true"}, "DEBUG"),
        (
            {"DJANGO_SETTINGS_MODULE": "config.settings.test"},
            "DJANGO_SETTINGS_MODULE",
        ),
        ({"SECRET_KEY": "development-only-secret-key"}, "SECRET_KEY"),
        (
            {"CLINIC_SECRET_BACKEND": "", "CLINIC_SECRET_DIR": ""},
            "CLINIC_SECRET",
        ),
        ({"CLINIC_RELEASE_ID": ""}, "CLINIC_RELEASE_ID"),
        (
            {"CLINIC_LIVE_ACTIVATION_APPROVAL": ""},
            "CLINIC_LIVE_ACTIVATION_APPROVAL",
        ),
    ],
)
def test_live_preflight_rejects_invalid_environment(
    tmp_path: Path, drift: dict[str, str], expected: str
) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    environment.update(drift)
    code, report = activation._preflight_report(environment)
    assert code == 1
    assert report["ready"] is False
    assert any(expected in finding for finding in report["environment_findings"])


def test_live_preflight_rejects_synthetic_storage(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    environment["APP_DATABASE_URL"] = (
        environment["APP_DATABASE_URL"]
        .replace("/clinic_live?", "/clinic?")
        .replace("db.qa.clinic-os.dev", "localhost")
    )
    code, report = activation._preflight_report(environment)
    assert code == 1
    assert any(
        "synthetic default" in finding for finding in report["environment_findings"]
    )

    environment = _live_environment(tmp_path, root)
    environment["EHR_ATTACHMENT_ROOT"] = str(
        activation.SYNTHETIC_DEFAULT_ATTACHMENT_ROOT
    )
    code, report = activation._preflight_report(environment)
    assert code == 1
    assert any(
        "synthetic default" in finding for finding in report["environment_findings"]
    )


def test_activate_writes_bound_record(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    code, report = activation._activate_report(environment)
    assert code == 0
    assert report["activated"] is True
    assert report["rehearsal"] is True
    record = _read_state(environment)
    assert set(record) == activation.RECORD_FIELDS
    assert record["status"] == "active"
    assert record["release_id"] == RELEASE_ID
    assert record["environment"] == "live"
    assert record["system"] == readiness.SYSTEM_IDENTIFIER
    assert record["approval_reference"] == APPROVAL
    assert record["disabled_at"] is None
    assert record["rehearsal"] is True
    assert record["evidence_root"] == str(root.resolve())
    assert record["storage"]["database_name"] == "clinic_live"
    assert record["storage"]["database_host"] == "db.qa.clinic-os.dev"
    assert record["storage"]["database_port"] == "5432"
    assert record["storage"]["broker_url"] == environment["CELERY_BROKER_URL"]
    assert record["storage"]["attachment_root"] == str(
        Path(environment["EHR_ATTACHMENT_ROOT"]).resolve()
    )


def test_activate_is_idempotent_for_same_binding(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    code, report = activation._activate_report(environment)
    assert code == 0
    assert report["idempotent"] is True
    assert report["rehearsal"] is True


def test_activate_refuses_without_approval(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    environment[activation.ACTIVATION_APPROVAL_ENV] = ""
    code, report = activation._activate_report(environment)
    assert code == 1
    assert report["activated"] is False
    assert any(
        activation.ACTIVATION_APPROVAL_ENV in finding for finding in report["findings"]
    )
    assert not _state_path(environment).exists()


def test_activate_refuses_in_synthetic_mode(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    environment["CLINIC_DATA_MODE"] = "synthetic"
    code, _report = activation._activate_report(environment)
    assert code == 2
    assert not _state_path(environment).exists()


def test_activate_refuses_when_evidence_not_ready(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    (root / "records" / "managed_secrets.record.json").unlink()
    environment = _live_environment(tmp_path, root)
    code, report = activation._activate_report(environment)
    assert code == 1
    assert report["activated"] is False
    assert any("not ready" in finding for finding in report["findings"])
    assert not _state_path(environment).exists()


def test_activate_refuses_second_active_release(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    original = _state_path(environment).read_bytes()
    environment[readiness.RELEASE_ID_ENV] = "test-release-other"
    code, report = activation._activate_report(environment)
    assert code == 1
    assert report["activated"] is False
    assert any("already active" in finding for finding in report["findings"])
    assert _state_path(environment).read_bytes() == original


def test_activate_refuses_after_evidence_drift(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    original = _state_path(environment).read_bytes()
    (root / "evidence" / "pix.json").write_bytes(b"tampered evidence bytes")
    code, report = activation._activate_report(environment)
    assert code == 1
    assert report["activated"] is False
    assert _state_path(environment).read_bytes() == original


def test_interrupted_activation_leaves_no_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)

    def _boom(source: Path, target: Path) -> None:
        raise OSError

    monkeypatch.setattr(Path, "replace", _boom)
    code, report = activation._activate_report(environment)
    assert code == 2
    assert report["activated"] is False
    assert not _state_path(environment).exists()
    assert not list(tmp_path.glob(".activation.json.*.tmp"))


def test_disable_rolls_back_and_preserves_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _approved_backend_environment(
        monkeypatch, _live_environment(tmp_path, root)
    )
    assert activation._activate_report(environment)[0] == 0
    evidence_snapshot = {
        path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }
    code, report = activation._disable_report(environment)
    assert code == 0
    assert report["disabled"] is True
    record = _read_state(environment)
    assert record["status"] == "disabled"
    assert record["disabled_at"] is not None
    assert record["release_id"] == RELEASE_ID
    assert record["evidence_manifest_sha256"]
    assert {
        path.relative_to(root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    } == evidence_snapshot
    with pytest.raises(ImproperlyConfigured, match="not active"):
        activation.require_live_activation(environment)
    code, again = activation._disable_report(environment)
    assert code == 0
    assert again["idempotent"] is True
    code, reactivated = activation._activate_report(environment)
    assert code == 0
    assert reactivated["activated"] is True
    assert _read_state(environment)["status"] == "active"


def test_disable_without_record_is_idempotent(tmp_path: Path) -> None:
    environment = {
        activation.ACTIVATION_STATE_ENV: str(tmp_path / "absent.json"),
    }
    code, report = activation._disable_report(environment)
    assert code == 0
    assert report["disabled"] is True
    assert report["idempotent"] is True


def test_disable_refuses_corrupt_record(tmp_path: Path) -> None:
    state = tmp_path / "activation.json"
    state.write_text("{not json")
    environment = {activation.ACTIVATION_STATE_ENV: str(state)}
    code, report = activation._disable_report(environment)
    assert code == 1
    assert report["disabled"] is False
    assert state.read_text() == "{not json"


def test_disable_write_failure_preserves_active_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    original = _state_path(environment).read_bytes()

    def _boom(source: Path, target: Path) -> None:
        raise OSError

    monkeypatch.setattr(Path, "replace", _boom)
    code, report = activation._disable_report(environment)
    assert code == 1
    assert report["disabled"] is False
    assert _state_path(environment).read_bytes() == original


def test_rehearsal_activation_never_authorizes_live_startup(
    tmp_path: Path,
) -> None:
    """The rehearsal surface exercises the transition, never live mode.

    ``activate`` under the opt-in writes a ``rehearsal: true`` record, but
    production startup refuses it twice over: the flag itself violates the
    live environment contract and the record is a rehearsal. Dropping the
    flag still fails on the rehearsal-only secret backend.
    """
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    code, report = activation._activate_report(environment)
    assert code == 0
    assert report["rehearsal"] is True
    assert _read_state(environment)["rehearsal"] is True
    result = _settings_probe(environment)
    assert result.returncode != 0
    assert "not approved" in result.stderr
    assert "rehearsal" in result.stderr
    unflagged = dict(environment)
    unflagged[activation.LIVE_REHEARSAL_ENV] = ""
    result = _settings_probe(unflagged)
    assert result.returncode != 0
    assert "rehearsal-only" in result.stderr


def test_rehearsal_flag_never_authorizes_live_without_activation(
    tmp_path: Path,
) -> None:
    """The flag alone is a violation, not a prerequisite substitute."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    result = _settings_probe(environment)
    assert result.returncode != 0
    assert "not approved" in result.stderr
    assert activation.LIVE_REHEARSAL_ENV in result.stderr


def test_rehearsal_record_cannot_authorize_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with an approved backend, a rehearsal record never binds live."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    approved = _approved_backend_environment(monkeypatch, environment)
    with pytest.raises(ImproperlyConfigured, match="rehearsal"):
        activation.require_live_activation(approved)


def test_approved_activation_authorizes_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The authorized side of the boundary under a simulated approved backend."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _approved_backend_environment(
        monkeypatch, _live_environment(tmp_path, root)
    )
    code, report = activation._activate_report(environment)
    assert code == 0
    assert report["rehearsal"] is False
    assert _read_state(environment)["rehearsal"] is False
    assert activation.require_live_activation(environment) == "live"


def test_approved_activation_binding_drift_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record-binding findings stay reachable under an approved backend."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _approved_backend_environment(
        monkeypatch, _live_environment(tmp_path, root)
    )
    assert activation._activate_report(environment)[0] == 0
    record = _read_state(environment)
    drifted = dict(environment)
    drifted[readiness.RELEASE_ID_ENV] = "test-release-other"
    assert any(
        "release_id" in finding
        for finding in activation.activation_record_findings(
            record, drifted, activation._now()
        )
    )
    drifted = dict(environment)
    drifted["CELERY_BROKER_URL"] = "redis://127.0.0.1:6379/10"
    assert any(
        "broker binding" in finding
        for finding in activation.activation_record_findings(
            record, drifted, activation._now()
        )
    )
    drifted = dict(environment)
    drifted["APP_DATABASE_URL"] = environment["APP_DATABASE_URL"].replace(
        ":5432/", ":6432/"
    )
    assert any(
        "database binding" in finding
        for finding in activation.activation_record_findings(
            record, drifted, activation._now()
        )
    )


def test_live_startup_refused_without_activation(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    result = _settings_probe(environment)
    assert result.returncode != 0
    assert "live data mode is not approved" in result.stderr


def test_live_startup_refused_after_disable(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    assert activation._disable_report(environment)[0] == 0
    result = _settings_probe(environment)
    assert result.returncode != 0
    assert "not approved" in result.stderr


def test_live_startup_refused_with_corrupt_record(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    _state_path(environment).write_text("{not json")
    result = _settings_probe(environment)
    assert result.returncode != 0
    assert "not approved" in result.stderr


def test_live_startup_refused_when_release_id_drifts(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    drifted = dict(environment)
    drifted[readiness.RELEASE_ID_ENV] = "test-release-other"
    result = _settings_probe(drifted)
    assert result.returncode != 0
    assert "not approved" in result.stderr


def test_live_startup_refused_when_evidence_drifts(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    (root / "evidence" / "pix.json").write_bytes(b"tampered evidence bytes")
    result = _settings_probe(environment)
    assert result.returncode != 0
    assert "not approved" in result.stderr


def test_live_startup_refused_when_storage_drifts(tmp_path: Path) -> None:
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    other = tmp_path / "other-attachments"
    other.mkdir()
    drifted = dict(environment)
    drifted["EHR_ATTACHMENT_ROOT"] = str(other)
    result = _settings_probe(drifted)
    assert result.returncode != 0
    assert "not approved" in result.stderr


def test_synthetic_startup_refused_on_equivalent_claimed_urls(
    tmp_path: Path,
) -> None:
    """Equivalent DSN spellings cannot bypass a live endpoint claim.

    The isolation identity is derived from the effective ``env.db``
    configuration merged the way the PostgreSQL driver's
    ``get_connection_params`` merges it, so every spelling the synthetic
    settings accept resolves to the claimed endpoint: the
    ``postgres``/``psql``/``pgsql`` scheme aliases, an omitted port
    defaulting to 5432, percent-encoded database names, case-folded
    hosts, any member of a cluster DSN, connection-routing OPTIONS —
    a ``dbname`` query option overriding the URL path name, a ``host``
    option supplying the authority the URL omitted, and a ``hostaddr``
    option redirecting the dialed address — and equivalent address/port
    spellings: IPv4-mapped IPv6 literals dial the embedded IPv4
    endpoint, non-canonical IPv4 spellings resolve to the same address,
    and zero-padded or list ports are the same port. Host and hostaddr
    lists pair element-wise with an empty hostaddr member falling back
    to its paired host member, so a partially empty hostaddr list
    cannot hide a claimed host.
    """
    claimed = ("db.qa.clinic-os.dev", "5432", "clinic_live")
    claimed_addr = ("10.20.30.40", "5432", "clinic_live")
    environment = {activation.ACTIVATION_STATE_ENV: str(tmp_path / "activation.json")}
    activation._claim_live_database(environment, claimed)
    activation._claim_live_database(environment, claimed_addr)
    equivalent_urls = [
        "postgresql://clinic_app:pw@db.qa.clinic-os.dev:5432/clinic_live",
        "postgres://clinic_app:pw@db.qa.clinic-os.dev/clinic_live",
        "psql://clinic_app:pw@db.qa.clinic-os.dev/clinic_live",
        "pgsql://clinic_app:pw@db.qa.clinic-os.dev:5432/clinic_live",
        "postgresql://clinic_app:pw@db.qa.clinic-os.dev/clinic_live",
        "postgresql://clinic_app:pw@db.qa.clinic-os.dev:5432/%63linic_live",
        "postgresql://clinic_app:pw@DB.QA.CLINIC-OS.DEV:5432/clinic_live",
        "postgres://clinic_app:pw@db.other.clinic-os.dev:6432,"
        "db.qa.clinic-os.dev:5432/clinic_live",
        # OPTIONS dbname overrides the unclaimed URL path name.
        "postgresql://clinic_app:pw@db.qa.clinic-os.dev:5432/"
        "unclaimed_name?dbname=clinic_live",
        # OPTIONS host/port fill an empty URL authority.
        "postgres://clinic_app:pw@/clinic_live?host=db.qa.clinic-os.dev",
        "postgres://clinic_app:pw@/clinic_live?host=db.qa.clinic-os.dev&port=5432",
        # OPTIONS hostaddr redirects the dialed address.
        "postgresql://clinic_app:pw@unclaimed.invalid:5432/clinic_live"
        "?hostaddr=10.20.30.40",
        "postgres://clinic_app:pw@/clinic_live?hostaddr=10.20.30.40",
        # IPv4-mapped IPv6 hostaddr dials the embedded IPv4 endpoint.
        "postgresql://clinic_app:pw@unclaimed.invalid:5432/clinic_live"
        "?hostaddr=::ffff:10.20.30.40",
        "postgres://clinic_app:pw@/clinic_live?hostaddr=::ffff:10.20.30.40",
        # A mapped IPv6 literal in the URL authority is the same endpoint.
        "postgresql://clinic_app:pw@[::ffff:10.20.30.40]:5432/clinic_live",
        # Non-canonical IPv4 spellings resolve to the same address.
        "postgres://clinic_app:pw@/clinic_live?hostaddr=0xa141e28",
        "postgres://clinic_app:pw@/clinic_live?host=10.20.30.40.",
        # Zero-padded and list ports are the same port.
        "postgres://clinic_app:pw@/clinic_live"
        "?host=10.20.30.40,10.20.30.40&port=05432,05432",
        "postgres://clinic_app:pw@/clinic_live?hostaddr=10.20.30.40&port=05432",
        # An empty hostaddr member falls back to the paired host member.
        "postgres://clinic_app:pw@/clinic_live"
        "?host=db.qa.clinic-os.dev,unclaimed.invalid&hostaddr=,10.20.30.41",
        "postgres://clinic_app:pw@/clinic_live"
        "?host=unclaimed.invalid,db.qa.clinic-os.dev&hostaddr=10.20.30.41,",
    ]
    for url in equivalent_urls:
        findings = activation.synthetic_isolation_findings(
            {"APP_DATABASE_URL": url, "EHR_ATTACHMENT_ROOT": str(tmp_path)}
        )
        assert any("claimed by a live activation" in finding for finding in findings), (
            url,
            findings,
        )
    unclaimed = "postgres://clinic_app:pw@db.qa.clinic-os.dev/clinic_other"
    assert (
        activation.synthetic_isolation_findings(
            {"APP_DATABASE_URL": unclaimed, "EHR_ATTACHMENT_ROOT": str(tmp_path)}
        )
        == []
    )
    # The same empty-member fallback is allowed when the paired host
    # member is genuinely unclaimed.
    unclaimed_fallback = (
        "postgres://clinic_app:pw@/clinic_live"
        "?host=unclaimed.invalid,192.0.2.99&hostaddr=,10.20.30.41"
    )
    assert (
        activation.synthetic_isolation_findings(
            {
                "APP_DATABASE_URL": unclaimed_fallback,
                "EHR_ATTACHMENT_ROOT": str(tmp_path),
            }
        )
        == []
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgres://clinic_app:pw@/clinic_live",
        "postgres://clinic_app:pw@db.qa.clinic-os.dev",
        "postgres://clinic_app:pw@db.qa.clinic-os.dev:notaport/clinic_live",
        # pg_service.conf indirection hides the effective target.
        "postgres://clinic_app:pw@db.qa.clinic-os.dev:5432/clinic_live?service=qa",
        "postgres://clinic_app:pw@/clinic_live?service=qa",
        # A hostaddr list libpq cannot pair with the host list.
        "postgres://clinic_app:pw@db.qa.clinic-os.dev/clinic_live"
        "?hostaddr=10.20.30.40,10.20.30.41",
        # A single hostaddr cannot pair with a multi-host list either.
        "postgres://clinic_app:pw@db.other.clinic-os.dev,"
        "db.third.clinic-os.dev/clinic_live?hostaddr=10.20.30.40",
        # A port list libpq cannot pair with the target list.
        "postgres://clinic_app:pw@db.other.clinic-os.dev,"
        "db.third.clinic-os.dev,db.fourth.clinic-os.dev/clinic_live"
        "?port=5432,5433",
        # An undialable port number fails closed, not as unclaimed.
        "postgres://clinic_app:pw@/clinic_live?host=db.qa.clinic-os.dev&port=99999",
        # An empty dial member selects the default unix-socket behavior,
        # which bypasses host comparison: fail closed, not unclaimed.
        "postgres://clinic_app:pw@/clinic_live?host=,db.qa.clinic-os.dev",
        "postgres://clinic_app:pw@/clinic_live?hostaddr=,10.20.30.40",
        "postgres://clinic_app:pw@/clinic_live"
        "?host=,db.qa.clinic-os.dev&hostaddr=,10.20.30.41",
    ],
)
def test_synthetic_startup_fails_closed_on_unresolvable_database_url(
    tmp_path: Path, url: str
) -> None:
    """A supported URL whose endpoint cannot be resolved is never unclaimed.

    A unix-socket DSN bypasses host comparison, a missing database name
    fails Django's NAME check, an invalid port fails the settings parser,
    a ``service`` option delegates routing to pg_service.conf, a
    host/hostaddr arity libpq cannot pair has no modelable target, and an
    empty host or hostaddr member selects the default unix-socket
    behavior — none may pass the isolation check as unclaimed.
    """
    findings = activation.synthetic_isolation_findings(
        {"APP_DATABASE_URL": url, "EHR_ATTACHMENT_ROOT": str(tmp_path)}
    )
    assert findings


def test_synthetic_startup_refused_on_environment_routed_urls(
    tmp_path: Path,
) -> None:
    """libpq environment routing cannot bypass a live endpoint claim.

    The driver forwards only the parameters the DSN sets, so libpq fills
    the rest from the process environment: ``PGHOSTADDR`` reroutes even
    an explicit URL host — including a partially empty list whose empty
    member falls back to the paired host member — ``PGHOST``/``PGPORT``
    supply an empty authority's endpoint, and ``PGSERVICE`` delegates
    routing to pg_service.conf — which cannot be resolved here, so it
    fails closed instead of passing as unclaimed.
    """
    claimed = ("db.qa.clinic-os.dev", "5432", "clinic_live")
    claimed_addr = ("10.20.30.40", "5432", "clinic_live")
    environment = {activation.ACTIVATION_STATE_ENV: str(tmp_path / "activation.json")}
    activation._claim_live_database(environment, claimed)
    activation._claim_live_database(environment, claimed_addr)
    routed = [
        # PGHOSTADDR overrides the address libpq dials for any host.
        (
            "postgresql://clinic_app:pw@unclaimed.invalid:5432/clinic_live",
            {"PGHOSTADDR": "10.20.30.40"},
        ),
        (
            "postgresql://clinic_app:pw@unclaimed.invalid:5432/clinic_live",
            {"PGHOSTADDR": "::ffff:10.20.30.40"},
        ),
        # PGHOST/PGPORT fill parameters the DSN leaves unset.
        (
            "postgres://clinic_app:pw@/clinic_live",
            {"PGHOST": "db.qa.clinic-os.dev"},
        ),
        (
            "postgres://clinic_app:pw@/clinic_live",
            {"PGHOST": "db.qa.clinic-os.dev", "PGPORT": "05432"},
        ),
        (
            "postgres://clinic_app:pw@/clinic_live",
            {"PGHOSTADDR": "10.20.30.40", "PGPORT": "5432"},
        ),
        # An empty PGHOSTADDR member falls back to the paired host member.
        (
            "postgres://clinic_app:pw@/clinic_live"
            "?host=db.qa.clinic-os.dev,unclaimed.invalid",
            {"PGHOSTADDR": ",10.20.30.41"},
        ),
    ]
    for url, extra in routed:
        findings = activation.synthetic_isolation_findings(
            {
                "APP_DATABASE_URL": url,
                "EHR_ATTACHMENT_ROOT": str(tmp_path),
                **extra,
            }
        )
        assert any("claimed by a live activation" in finding for finding in findings), (
            url,
            extra,
            findings,
        )
    # pg_service.conf routing cannot be resolved: it fails closed.
    findings = activation.synthetic_isolation_findings(
        {
            "APP_DATABASE_URL": (
                "postgres://clinic_app:pw@unclaimed.invalid:5432/clinic_live"
            ),
            "EHR_ATTACHMENT_ROOT": str(tmp_path),
            "PGSERVICE": "qa",
        }
    )
    assert findings
    # An unclaimed endpoint reached through the same routing is allowed.
    findings = activation.synthetic_isolation_findings(
        {
            "APP_DATABASE_URL": "postgres://clinic_app:pw@/clinic_other",
            "EHR_ATTACHMENT_ROOT": str(tmp_path),
            "PGHOST": "db.qa.clinic-os.dev",
        }
    )
    assert findings == []


def test_synthetic_startup_refused_on_proxied_claimed_urls(
    tmp_path: Path,
) -> None:
    """``$VARIABLE`` indirection cannot bypass a live endpoint claim.

    ``env.db`` resolves ``APP_DATABASE_URL`` through ``get_value`` first:
    a value starting with ``$`` names another environment variable whose
    own value is resolved recursively, so the synthetic settings open
    the endpoint the proxy chain ends at — not the literal ``$VAR``
    string. The isolation check resolves the same effective
    configuration, so a direct or nested proxy to a claimed endpoint is
    refused exactly like the literal URL. A proxy chain that cannot
    resolve (a cycle) fails closed instead of passing as unclaimed; a
    missing proxy target falls back to the synthetic default exactly
    like ``env.db`` does, and a proxy to a non-PostgreSQL engine still
    yields no identity.
    """
    claimed = ("db.qa.clinic-os.dev", "5432", "clinic_live")
    environment = {activation.ACTIVATION_STATE_ENV: str(tmp_path / "activation.json")}
    activation._claim_live_database(environment, claimed)
    claimed_url = "postgresql://clinic_app:pw@db.qa.clinic-os.dev:5432/clinic_live"
    proxied = [
        # A direct proxy to the claimed URL.
        {"APP_DATABASE_URL": "$T46_DATABASE_URL", "T46_DATABASE_URL": claimed_url},
        # A nested proxy chain resolving to the claimed URL.
        {
            "APP_DATABASE_URL": "$T46_DATABASE_ALIAS",
            "T46_DATABASE_ALIAS": "$T46_DATABASE_URL",
            "T46_DATABASE_URL": claimed_url,
        },
        # Repeated leading markers strip the same way get_value strips.
        {"APP_DATABASE_URL": "$$T46_DATABASE_URL", "T46_DATABASE_URL": claimed_url},
    ]
    for extra in proxied:
        findings = activation.synthetic_isolation_findings(
            {"EHR_ATTACHMENT_ROOT": str(tmp_path), **extra}
        )
        assert any("claimed by a live activation" in finding for finding in findings), (
            extra,
            findings,
        )
    # A proxy cycle cannot resolve to an endpoint: fail closed.
    findings = activation.synthetic_isolation_findings(
        {
            "APP_DATABASE_URL": "$T46_LOOP_A",
            "T46_LOOP_A": "$T46_LOOP_B",
            "T46_LOOP_B": "$T46_LOOP_A",
            "EHR_ATTACHMENT_ROOT": str(tmp_path),
        }
    )
    assert findings
    # A missing proxy target falls back to the synthetic default, which
    # is unclaimed here — the same endpoint env.db would open.
    findings = activation.synthetic_isolation_findings(
        {
            "APP_DATABASE_URL": "$T46_MISSING_URL",
            "EHR_ATTACHMENT_ROOT": str(tmp_path),
        }
    )
    assert findings == []
    # A proxy to a non-PostgreSQL engine still yields no identity.
    findings = activation.synthetic_isolation_findings(
        {
            "APP_DATABASE_URL": "$T46_DATABASE_URL",
            "T46_DATABASE_URL": "sqlite:///:memory:",
            "EHR_ATTACHMENT_ROOT": str(tmp_path),
        }
    )
    assert findings == []


def test_synthetic_startup_refused_on_proxied_claimed_roots(
    tmp_path: Path,
) -> None:
    """``$VARIABLE`` indirection cannot bypass a live attachment claim.

    ``env`` resolves ``EHR_ATTACHMENT_ROOT`` through ``get_value`` first:
    a value starting with ``$`` names another environment variable whose
    own value is resolved recursively, so the synthetic settings open the
    root the proxy chain ends at — not the literal ``$VAR`` path. The
    isolation check resolves the same effective root through the shared
    ``resolve_attachment_root`` seam, so a direct, nested or
    repeated-marker proxy to a live-claimed root is refused exactly like
    the literal path — by the unconditional marker check and, when the
    activation-state pointer is retained, by the record-binding check. A
    proxy chain that cannot resolve (a cycle) fails closed instead of
    passing as unclaimed; a missing proxy target falls back to the
    synthetic default exactly like ``env`` does, and a proxy to an
    unclaimed root is allowed.
    """
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    claimed = environment["EHR_ATTACHMENT_ROOT"]
    unclaimed = tmp_path / "synthetic-attachments"
    unclaimed.mkdir()
    proxied = [
        # A direct proxy to the claimed root.
        {
            "EHR_ATTACHMENT_ROOT": "$T46_ATTACHMENT_ROOT",
            "T46_ATTACHMENT_ROOT": claimed,
        },
        # A nested proxy chain resolving to the claimed root.
        {
            "EHR_ATTACHMENT_ROOT": "$T46_ATTACHMENT_ALIAS",
            "T46_ATTACHMENT_ALIAS": "$T46_ATTACHMENT_ROOT",
            "T46_ATTACHMENT_ROOT": claimed,
        },
        # Repeated leading markers strip the same way get_value strips.
        {
            "EHR_ATTACHMENT_ROOT": "$$T46_ATTACHMENT_ROOT",
            "T46_ATTACHMENT_ROOT": claimed,
        },
    ]
    for extra in proxied:
        findings = activation.synthetic_isolation_findings(
            {"APP_DATABASE_URL": "sqlite:///:memory:", **extra}
        )
        assert any("claimed by live storage" in finding for finding in findings), (
            extra,
            findings,
        )
        # With the state pointer retained, the record-binding check
        # resolves the same effective root.
        findings = activation.synthetic_isolation_findings(
            {
                "APP_DATABASE_URL": "sqlite:///:memory:",
                activation.ACTIVATION_STATE_ENV: environment[
                    activation.ACTIVATION_STATE_ENV
                ],
                **extra,
            }
        )
        assert any(
            "matches the attachment root bound" in finding for finding in findings
        ), (extra, findings)
    # A proxy cycle cannot resolve to a root: fail closed.
    findings = activation.synthetic_isolation_findings(
        {
            "APP_DATABASE_URL": "sqlite:///:memory:",
            "EHR_ATTACHMENT_ROOT": "$T46_LOOP_A",
            "T46_LOOP_A": "$T46_LOOP_B",
            "T46_LOOP_B": "$T46_LOOP_A",
        }
    )
    assert findings
    # A missing proxy target falls back to the synthetic default, which
    # is unclaimed here — the same root env would open.
    findings = activation.synthetic_isolation_findings(
        {
            "APP_DATABASE_URL": "sqlite:///:memory:",
            "EHR_ATTACHMENT_ROOT": "$T46_MISSING_ROOT",
        }
    )
    assert findings == []
    # A proxy to an unclaimed root is allowed.
    findings = activation.synthetic_isolation_findings(
        {
            "APP_DATABASE_URL": "sqlite:///:memory:",
            "EHR_ATTACHMENT_ROOT": "$T46_ATTACHMENT_ROOT",
            "T46_ATTACHMENT_ROOT": str(unclaimed),
        }
    )
    assert findings == []


def test_synthetic_startup_refused_on_dns_aliased_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DNS alias for a claimed address is the same claimed endpoint.

    The claim registry keys the endpoint, not one spelling of it: a
    claim on an address refuses every hostname resolving to it, and a
    claim on a hostname also claims every address it resolves to. The
    resolver is patched in-process to owned TEST-NET addresses so the
    mapping is deterministic — no public DNS dependency.
    """
    real_getaddrinfo = socket.getaddrinfo
    aliases = {
        "db-a.t46.test": "198.51.100.10",
        "db-b.t46.test": "198.51.100.10",
        "db-c.t46.test": "198.51.100.20",
    }

    def _resolve(
        host: bytes | str | None,
        port: bytes | str | int | None = 0,
        *args: int,
        **kwargs: int,
    ) -> Sequence[tuple[object, ...]]:
        mapped = aliases.get(host, host) if isinstance(host, str) else host
        return real_getaddrinfo(mapped, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    environment = {activation.ACTIVATION_STATE_ENV: str(tmp_path / "activation.json")}
    activation._claim_live_database(
        environment, ("db-a.t46.test", "5432", "clinic_live")
    )
    activation._claim_live_database(
        environment, ("198.51.100.20", "5432", "clinic_live")
    )
    refused = [
        # The literal claimed name.
        "postgresql://clinic_app:pw@db-a.t46.test:5432/clinic_live",
        # A different name resolving to the claimed address.
        "postgresql://clinic_app:pw@db-b.t46.test:5432/clinic_live",
        # The bare claimed address behind both names.
        "postgresql://clinic_app:pw@198.51.100.10:5432/clinic_live",
        # A name resolving to an address claimed by its literal spelling.
        "postgresql://clinic_app:pw@db-c.t46.test:5432/clinic_live",
        # hostaddr routing through an alias dials the claimed address.
        "postgresql://clinic_app:pw@unclaimed.invalid:5432/clinic_live"
        "?hostaddr=db-b.t46.test",
    ]
    for url in refused:
        findings = activation.synthetic_isolation_findings(
            {"APP_DATABASE_URL": url, "EHR_ATTACHMENT_ROOT": str(tmp_path)}
        )
        assert any("claimed by a live activation" in finding for finding in findings), (
            url,
            findings,
        )
    # Same names, different database: no claim matches.
    unclaimed = "postgresql://clinic_app:pw@db-b.t46.test:5432/clinic_other"
    assert (
        activation.synthetic_isolation_findings(
            {"APP_DATABASE_URL": unclaimed, "EHR_ATTACHMENT_ROOT": str(tmp_path)}
        )
        == []
    )


def test_live_environment_fails_closed_on_unresolvable_database_host(
    tmp_path: Path,
) -> None:
    """A live database host that cannot be resolved is refused.

    An unresolvable host cannot claim the storage it would reach once
    the name resolves, so the live contract fails closed instead of
    binding an endpoint whose reachable spellings are unknown.
    """
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    environment["APP_DATABASE_URL"] = environment["APP_DATABASE_URL"].replace(
        "db.qa.clinic-os.dev", "db.unresolvable.invalid"
    )
    code, report = activation._preflight_report(environment)
    assert code == 1
    assert any(
        "could not be resolved" in finding for finding in report["environment_findings"]
    )


def test_live_environment_refuses_libpq_reroute_variables(
    tmp_path: Path,
) -> None:
    """Live binds the approved DSN endpoint; env routing must not override.

    ``PGHOSTADDR`` overrides the address libpq dials even for an explicit
    DSN host and ``PGSERVICE`` delegates routing to pg_service.conf, so
    either would let a live process dial an endpoint the claim does not
    cover — the live contract refuses both.
    """
    root = _live_bundle(tmp_path / "evidence")
    for variable in ("PGHOSTADDR", "PGSERVICE"):
        environment = _live_environment(tmp_path, root)
        environment[variable] = "10.20.30.40"
        code, report = activation._preflight_report(environment)
        assert code == 1
        assert any(variable in finding for finding in report["environment_findings"]), (
            variable,
            report,
        )


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker unavailable")
def test_synthetic_startup_refused_on_claimed_database_with_sentinels(
    tmp_path: Path,
) -> None:
    """Equivalent DSN spellings cannot reach a claimed, sentinel-held database.

    Database-backed regression for the equivalent-URL bypass: activation
    claims a provisioned PostgreSQL endpoint, then each synthetic
    ``config.settings.base`` process below would read and insert sentinel
    rows if the isolation check missed its URL spelling — the
    ``postgres`` scheme with the default port, a percent-encoded database
    name, a ``dbname`` option overriding the path name, a ``hostaddr``
    option redirecting the dialed address, an IPv4-mapped ``hostaddr``,
    a zero-padded port list, ``PGHOSTADDR`` environment routing,
    partially empty ``hostaddr`` lists whose empty member falls back to
    the paired host member (query-option and ``PGHOSTADDR`` forms),
    ``$VARIABLE`` proxy indirection — direct and nested — resolving to
    the claimed endpoint the way ``env.db`` resolves it, and
    DNS aliases resolving to the claimed address (the probe patches
    ``socket.getaddrinfo`` so the alias mapping is deterministic and
    owned by the test, not public DNS). The claim persists after
    ``disable``, so every spelling must still refuse. The owner
    connection verifies no probe wrote.
    """
    token = f"t46iso{os.urandom(4).hex()}"
    with _provision_database(REPOSITORY, token) as database:
        inspection = _run_process(
            (
                "docker",
                "inspect",
                "-f",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                database.container,
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        assert inspection.returncode == 0, inspection.stderr
        bridge_ip = inspection.stdout.strip()
        assert bridge_ip
        with psycopg.connect(database.owner_dsn, connect_timeout=5) as owner:
            owner.execute("CREATE TABLE clinic_app.t46_sentinel(value text NOT NULL)")
            owner.execute(
                "INSERT INTO clinic_app.t46_sentinel(value) "
                "VALUES ('preserved-original')"
            )
            owner.execute(
                "GRANT SELECT, INSERT ON clinic_app.t46_sentinel TO clinic_app"
            )
        root = _live_bundle(tmp_path / "evidence")
        environment = _live_environment(tmp_path, root)
        ca = str(tmp_path / "db-ca.pem")
        password = quote(database.app_password, safe="")
        tls = f"connect_timeout=2&sslmode=verify-full&sslrootcert={ca}"
        environment["APP_DATABASE_URL"] = (
            f"postgresql://clinic_app:{password}@{bridge_ip}:5432/clinic?{tls}"
        )
        code, report = activation._activate_report(environment)
        assert code == 0, report
        synthetic_root = tmp_path / "synthetic-attachments"
        synthetic_root.mkdir()
        alias = f"db-alias.{token}.test"
        probe = (
            "import socket as _socket, django, json;"  # noqa: S608
            "_real = _socket.getaddrinfo;"
            f"_socket.getaddrinfo = lambda host, port=0, *a, **k: "
            f"_real({bridge_ip!r} if host == {alias!r} else host, port, *a, **k);"
            "django.setup();"
            "from django.db import connection;"
            "cursor = connection.cursor();"
            "cursor.execute('SELECT value FROM clinic_app.t46_sentinel "
            "ORDER BY value');"
            "before = [row[0] for row in cursor.fetchall()];"
            "cursor.execute('INSERT INTO clinic_app.t46_sentinel(value) "
            "VALUES (%s)', ('probe-wrote',));"
            "print(json.dumps({'before': before}))"
        )
        cases = [
            (
                "exact_endpoint_control",
                f"postgresql://clinic_app:{password}@{bridge_ip}:5432/clinic?"
                "connect_timeout=2",
                {},
            ),
            (
                "postgres_default_port",
                f"postgres://clinic_app:{password}@{bridge_ip}/clinic?"
                "connect_timeout=2",
                {},
            ),
            (
                "encoded_database",
                f"postgresql://clinic_app:{password}@{bridge_ip}:5432/%63linic?"
                "connect_timeout=2",
                {},
            ),
            (
                "options_dbname",
                f"postgresql://clinic_app:{password}@{bridge_ip}:5432/"
                "unclaimed_name?dbname=clinic&connect_timeout=2",
                {},
            ),
            (
                "options_hostaddr",
                f"postgresql://clinic_app:{password}@unclaimed.invalid:5432/"
                f"clinic?hostaddr={bridge_ip}&connect_timeout=2",
                {},
            ),
            (
                "hostaddr_ipv4_mapped",
                f"postgresql://clinic_app:{password}@unclaimed.invalid:5432/"
                f"clinic?hostaddr=::ffff:{bridge_ip}&connect_timeout=2",
                {},
            ),
            (
                "options_port_list",
                f"postgresql://clinic_app:{password}@/clinic?"
                f"host={bridge_ip},{bridge_ip}&port=05432,05432"
                "&connect_timeout=2",
                {},
            ),
            (
                "environment_hostaddr",
                f"postgresql://clinic_app:{password}@unclaimed.invalid:5432/"
                "clinic?connect_timeout=2",
                {"PGHOSTADDR": bridge_ip},
            ),
            # A partially empty hostaddr list: the empty member falls
            # back to the paired host member, which is the claimed
            # endpoint — the round-7 bypass, query-option form.
            (
                "options_hostaddr_empty_member",
                f"postgresql://clinic_app:{password}@/clinic?"
                + urlencode(
                    {
                        "host": f"{bridge_ip},192.0.2.200",
                        "hostaddr": ",192.0.2.200",
                        "connect_timeout": "2",
                    }
                ),
                {},
            ),
            # The same partially empty list supplied through PGHOSTADDR.
            (
                "environment_hostaddr_empty_member",
                f"postgresql://clinic_app:{password}@/clinic?"
                + urlencode(
                    {
                        "host": f"{bridge_ip},192.0.2.200",
                        "connect_timeout": "2",
                    }
                ),
                {"PGHOSTADDR": ",192.0.2.200"},
            ),
            # A DNS alias resolving to the claimed address is the same
            # endpoint: the round-6 bypass.
            (
                "dns_alias",
                f"postgresql://clinic_app:{password}@{alias}:5432/clinic?"
                "connect_timeout=2",
                {},
            ),
            (
                "dns_alias_hostaddr",
                f"postgresql://clinic_app:{password}@unclaimed.invalid:5432/"
                f"clinic?hostaddr={alias}&connect_timeout=2",
                {},
            ),
            (
                "dns_alias_pghost",
                f"postgresql://clinic_app:{password}@/clinic?connect_timeout=2",
                {"PGHOST": alias},
            ),
            # django-environ's $VARIABLE indirection: env.db resolves the
            # proxy before parsing, so the synthetic process would open
            # the claimed endpoint the variable points at — the round-8
            # bypass, direct and nested forms.
            (
                "environment_proxy",
                "$T46_DATABASE_URL",
                {
                    "T46_DATABASE_URL": (
                        f"postgresql://clinic_app:{password}@{bridge_ip}:5432/"
                        "clinic?connect_timeout=2"
                    )
                },
            ),
            (
                "environment_nested_proxy",
                "$T46_DATABASE_ALIAS",
                {
                    "T46_DATABASE_ALIAS": "$T46_DATABASE_URL",
                    "T46_DATABASE_URL": (
                        f"postgresql://clinic_app:{password}@{bridge_ip}:5432/"
                        "clinic?connect_timeout=2"
                    ),
                },
            ),
        ]

        def _probe(label: str, dsn: str, extra: dict[str, str]) -> None:
            synthetic = {
                key: value
                for key, value in environment.items()
                if key != activation.ACTIVATION_STATE_ENV
            }
            synthetic.update(
                {
                    "APP_DATABASE_URL": dsn,
                    "CLINIC_DATA_MODE": "synthetic",
                    "DJANGO_SETTINGS_MODULE": "config.settings.base",
                    "EHR_ATTACHMENT_ROOT": str(synthetic_root),
                }
            )
            synthetic.update(extra)
            result = _run_process(
                (sys.executable, "-c", probe),
                cwd=REPOSITORY,
                env=_scrubbed(synthetic),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            assert result.returncode != 0, (label, result.stdout, result.stderr)
            assert "claimed by a live activation" in result.stderr, (
                label,
                result.stderr,
            )

        for label, dsn, extra in cases:
            _probe(label, dsn, extra)
        # The endpoint claim persists after disable — the database still
        # holds live-bound data — so every spelling must still refuse.
        assert activation._disable_report(environment)[0] == 0
        for label, dsn, extra in cases:
            _probe(f"after_disable_{label}", dsn, extra)
        with psycopg.connect(database.owner_dsn, connect_timeout=5) as owner:
            rows = [
                row[0]
                for row in owner.execute(
                    "SELECT value FROM clinic_app.t46_sentinel ORDER BY value"
                ).fetchall()
            ]
        assert rows == ["preserved-original"]


def test_synthetic_startup_ignores_unrelated_activation_state(
    tmp_path: Path,
) -> None:
    """A record bound to other storage never blocks synthetic startup."""
    root = _live_bundle(tmp_path / "evidence")
    live_environment = _live_environment(tmp_path, root)
    assert activation._activate_report(live_environment)[0] == 0
    environment = {
        "CLINIC_DATA_MODE": "synthetic",
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        activation.ACTIVATION_STATE_ENV: live_environment[
            activation.ACTIVATION_STATE_ENV
        ],
    }
    result = _run_process(
        (sys.executable, "-c", "import config.settings.base"),
        cwd=REPOSITORY,
        env=_scrubbed(environment),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_synthetic_startup_refused_on_live_bound_storage(tmp_path: Path) -> None:
    """Synthetic mode can never start on storage a live activation bound."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    synthetic = dict(environment)
    synthetic["CLINIC_DATA_MODE"] = "synthetic"
    result = _settings_probe(synthetic)
    assert result.returncode != 0
    assert "live activation" in result.stderr


def test_synthetic_startup_refused_on_live_storage_without_state_pointer(
    tmp_path: Path,
) -> None:
    """Isolation does not depend on retaining the activation-state variable.

    A synthetic deployment is not required to carry live activation
    variables: the endpoint claim registry and the root ownership marker
    refuse live-bound storage on their own, for the same root, a different
    root, a missing state file and a relative state path alike.
    """
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    synthetic = {
        key: value
        for key, value in environment.items()
        if key != activation.ACTIVATION_STATE_ENV
    }
    synthetic["CLINIC_DATA_MODE"] = "synthetic"
    result = _settings_probe(synthetic)
    assert result.returncode != 0
    assert "not isolated" in result.stderr

    other_root = tmp_path / "synthetic-attachments"
    other_root.mkdir()
    synthetic["EHR_ATTACHMENT_ROOT"] = str(other_root)
    result = _settings_probe(synthetic)
    assert result.returncode != 0
    assert "claimed by a live activation" in result.stderr

    synthetic[activation.ACTIVATION_STATE_ENV] = str(tmp_path / "absent.json")
    result = _settings_probe(synthetic)
    assert result.returncode != 0
    assert "claimed by a live activation" in result.stderr

    synthetic[activation.ACTIVATION_STATE_ENV] = "relative/activation.json"
    result = _settings_probe(synthetic)
    assert result.returncode != 0
    assert "claimed by a live activation" in result.stderr


def test_synthetic_read_refused_on_live_claimed_root_without_state(
    tmp_path: Path,
) -> None:
    """Existing live attachment bytes stay unreadable without the pointer."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    Path(environment["EHR_ATTACHMENT_ROOT"], "d" * 64).write_bytes(b"live bytes")
    synthetic = {
        key: value
        for key, value in environment.items()
        if key != activation.ACTIVATION_STATE_ENV
    }
    synthetic["CLINIC_DATA_MODE"] = "synthetic"
    result = _run_process(
        (
            sys.executable,
            "-c",
            "from apps.ehr.attachment_storage import default_storage; "
            "print(default_storage().get('d' * 64))",
        ),
        cwd=REPOSITORY,
        env=_scrubbed(synthetic),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "claimed by live storage" in result.stderr


def test_synthetic_read_refused_on_proxied_live_claimed_root(
    tmp_path: Path,
) -> None:
    """``$VARIABLE`` indirection cannot bypass a live attachment claim.

    ``env`` resolves ``EHR_ATTACHMENT_ROOT`` before the adapter opens it:
    a value starting with ``$`` names another environment variable whose
    own value is resolved recursively, so a synthetic process whose
    configured root is a proxy chain ending at a live-claimed root must
    be refused at startup exactly like the literal path — before and
    after ``disable``. Each subprocess below runs real
    ``config.settings.base`` with an independent in-memory database and
    no activation-state pointer; without the refusal,
    ``default_storage().get`` would read live-bound bytes. A proxy to an
    unclaimed root still starts and reads its own object. The marker and
    the stored bytes are preserved throughout.
    """
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    claimed = Path(environment["EHR_ATTACHMENT_ROOT"])
    object_key = "d" * 64
    (claimed / object_key).write_bytes(b"live bytes")
    marker_path = activation._owner_marker_path(claimed.resolve())
    marker_bytes = marker_path.read_bytes()
    probe = (
        "import django, json;"
        "django.setup();"
        "from django.conf import settings;"
        "from apps.ehr.attachment_storage import default_storage;"
        "print(json.dumps({"
        "'mode': settings.CLINIC_DATA_MODE,"
        "'root': str(settings.EHR_ATTACHMENT_ROOT),"
        f"'object': default_storage().get({object_key!r}).decode()"
        "}))"
    )
    cases = [
        ("literal", {"EHR_ATTACHMENT_ROOT": str(claimed)}),
        (
            "direct_proxy",
            {
                "EHR_ATTACHMENT_ROOT": "$T46_ATTACHMENT_ROOT",
                "T46_ATTACHMENT_ROOT": str(claimed),
            },
        ),
        (
            "nested_proxy",
            {
                "EHR_ATTACHMENT_ROOT": "$T46_ATTACHMENT_ALIAS",
                "T46_ATTACHMENT_ALIAS": "$T46_ATTACHMENT_ROOT",
                "T46_ATTACHMENT_ROOT": str(claimed),
            },
        ),
        (
            "repeated_marker_proxy",
            {
                "EHR_ATTACHMENT_ROOT": "$$T46_ATTACHMENT_ROOT",
                "T46_ATTACHMENT_ROOT": str(claimed),
            },
        ),
    ]

    def _probe(label: str, extra: dict[str, str]) -> None:
        synthetic = {
            key: value
            for key, value in environment.items()
            if key != activation.ACTIVATION_STATE_ENV
        }
        synthetic.update(
            {
                "APP_DATABASE_URL": "sqlite:///:memory:",
                "CLINIC_DATA_MODE": "synthetic",
                "DJANGO_SETTINGS_MODULE": "config.settings.base",
            }
        )
        synthetic.update(extra)
        result = _run_process(
            (sys.executable, "-c", probe),
            cwd=REPOSITORY,
            env=_scrubbed(synthetic),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode != 0, (label, result.stdout, result.stderr)
        assert "claimed by live storage" in result.stderr, (label, result.stderr)

    for label, extra in cases:
        _probe(label, extra)
    # The marker persists after disable — the root still holds live-bound
    # bytes — so every proxy form must still refuse.
    assert activation._disable_report(environment)[0] == 0
    for label, extra in cases:
        _probe(f"after_disable_{label}", extra)
    # A proxy to an unclaimed root still starts and reads its own object.
    unclaimed = tmp_path / "synthetic-attachments"
    unclaimed.mkdir()
    (unclaimed / object_key).write_bytes(b"synthetic bytes")
    synthetic = {
        key: value
        for key, value in environment.items()
        if key != activation.ACTIVATION_STATE_ENV
    }
    synthetic.update(
        {
            "APP_DATABASE_URL": "sqlite:///:memory:",
            "CLINIC_DATA_MODE": "synthetic",
            "DJANGO_SETTINGS_MODULE": "config.settings.base",
            "EHR_ATTACHMENT_ROOT": "$T46_ATTACHMENT_ROOT",
            "T46_ATTACHMENT_ROOT": str(unclaimed),
        }
    )
    result = _run_process(
        (sys.executable, "-c", probe),
        cwd=REPOSITORY,
        env=_scrubbed(synthetic),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["mode"] == "synthetic"
    assert payload["root"] == str(unclaimed)
    assert payload["object"] == "synthetic bytes"
    assert marker_path.read_bytes() == marker_bytes
    assert (claimed / object_key).read_bytes() == b"live bytes"


def test_live_activation_refuses_database_claimed_by_another_activation(
    tmp_path: Path,
) -> None:
    """A second activation cannot take over an endpoint already claimed."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    other_root = tmp_path / "other-attachments"
    other_root.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = _live_environment(other_dir, root)
    other["EHR_ATTACHMENT_ROOT"] = str(other_root)
    code, report = activation._activate_report(other)
    assert code == 1
    assert report["activated"] is False
    assert any(
        "claimed by a different live activation" in finding
        for finding in report["findings"]
    )


def test_synthetic_write_refused_on_live_claimed_root(tmp_path: Path) -> None:
    """A synthetic process cannot write into a live-claimed root at runtime."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    store = FilesystemAttachmentStorage(Path(environment["EHR_ATTACHMENT_ROOT"]))
    with (
        override_settings(CLINIC_DATA_MODE="synthetic"),
        pytest.raises(AttachmentStorageError),
    ):
        store.put("c" * 64, b"synthetic in live storage")


def test_live_preflight_rejects_synthetic_claimed_root(tmp_path: Path) -> None:
    """Live refuses an attachment root already claimed by synthetic storage."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    activation.claim_synthetic_storage(Path(environment["EHR_ATTACHMENT_ROOT"]))
    code, report = activation._preflight_report(environment)
    assert code == 1
    assert any(
        "claimed by synthetic" in finding for finding in report["environment_findings"]
    )


def test_preflight_rejects_explicitly_empty_data_mode() -> None:
    """An explicitly empty CLINIC_DATA_MODE fails preflight, not defaults."""
    code, report = activation._preflight_report({"CLINIC_DATA_MODE": ""})
    assert code == 2
    assert "CLINIC_DATA_MODE" in report["error"]


def test_preflight_cli_rejects_explicitly_empty_data_mode() -> None:
    result = _cli("preflight", {"CLINIC_DATA_MODE": ""})
    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert "CLINIC_DATA_MODE" in report["error"]


def test_live_preflight_rejects_rehearsal_secret_backend(
    tmp_path: Path,
) -> None:
    """The rehearsal-only backend never satisfies the live prerequisite."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    environment[activation.LIVE_REHEARSAL_ENV] = ""
    code, report = activation._preflight_report(environment)
    assert code == 1
    assert report["rehearsal"] is False
    assert any(
        "rehearsal-only" in finding for finding in report["environment_findings"]
    )


def test_live_preflight_rejects_secret_store_without_material(
    tmp_path: Path,
) -> None:
    """An arbitrary empty directory is never live secret readiness."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    empty = tmp_path / "empty-secrets"
    empty.mkdir()
    environment["CLINIC_SECRET_DIR"] = str(empty)
    code, report = activation._preflight_report(environment)
    assert code == 1
    assert any(
        activation.REQUIRED_SECRET_NAME in finding
        for finding in report["environment_findings"]
    )


def test_live_startup_refused_when_database_port_drifts(tmp_path: Path) -> None:
    """A different port on the same host is a different approved endpoint."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    drifted = dict(environment)
    drifted["APP_DATABASE_URL"] = environment["APP_DATABASE_URL"].replace(
        ":5432/", ":6432/"
    )
    result = _settings_probe(drifted)
    assert result.returncode != 0
    assert "not approved" in result.stderr
    code, report = activation._preflight_report(drifted)
    assert code == 1
    assert any(
        "database binding" in finding for finding in report["environment_findings"]
    )


def test_live_startup_refused_when_broker_drifts(tmp_path: Path) -> None:
    """The job-dispatch endpoint is bound at activation too."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    drifted = dict(environment)
    drifted["CELERY_BROKER_URL"] = "redis://127.0.0.1:6379/10"
    result = _settings_probe(drifted)
    assert result.returncode != 0
    assert "not approved" in result.stderr


def test_live_startup_refused_when_rehearsal_flag_drifts(tmp_path: Path) -> None:
    """A rehearsal activation cannot run as a real activation or vice versa."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    assert activation._activate_report(environment)[0] == 0
    drifted = dict(environment)
    drifted[activation.LIVE_REHEARSAL_ENV] = ""
    result = _settings_probe(drifted)
    assert result.returncode != 0
    assert "rehearsal" in result.stderr


def test_live_preflight_rejects_loopback_database(tmp_path: Path) -> None:
    """Loopback aliases are never a live database endpoint."""
    root = _live_bundle(tmp_path / "evidence")
    environment = _live_environment(tmp_path, root)
    environment["APP_DATABASE_URL"] = environment["APP_DATABASE_URL"].replace(
        "db.qa.clinic-os.dev", "127.0.0.1"
    )
    code, report = activation._preflight_report(environment)
    assert code == 1
    assert any("loopback" in finding for finding in report["environment_findings"])


def _patch_environment(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]
) -> None:
    """Point the in-process runtime gate at one test environment."""
    for name in (
        "ALLOWED_HOSTS",
        "APP_DATABASE_URL",
        "CELERY_BROKER_URL",
        "CLINIC_DATA_MODE",
        "CLINIC_SECRET_BACKEND",
        "CLINIC_SECRET_DIR",
        "DJANGO_SETTINGS_MODULE",
        "EHR_ATTACHMENT_ROOT",
        "SECRET_KEY",
        "SECURE_SSL_HOST",
        activation.ACTIVATION_APPROVAL_ENV,
        activation.ACTIVATION_STATE_ENV,
        activation.LIVE_REHEARSAL_ENV,
        readiness.EVIDENCE_ROOT_ENV,
        readiness.RELEASE_ID_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


def test_disable_halts_running_live_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback stops new writes/jobs in an already-running live process.

    The process keeps its loaded settings; only the activation record
    changes. Existing objects stay readable; new writes, requests and job
    execution all fail closed.
    """
    root = _live_bundle(tmp_path / "evidence")
    environment = _approved_backend_environment(
        monkeypatch, _live_environment(tmp_path, root)
    )
    assert activation._activate_report(environment)[0] == 0
    _patch_environment(monkeypatch, environment)
    store = FilesystemAttachmentStorage(Path(environment["EHR_ATTACHMENT_ROOT"]))
    with override_settings(CLINIC_DATA_MODE="live"):
        store.put("a" * 64, b"before")
        assert activation._disable_report(environment)[0] == 0
        with pytest.raises(AttachmentStorageError):
            store.put("b" * 64, b"after")
        assert store.get("a" * 64) == b"before"
        with pytest.raises(AttachmentStorageError):
            store.get("b" * 64)

        halted = LiveModeHaltMiddleware(lambda _request: HttpResponse(status=200))
        request = HttpRequest()
        request.path_info = "/agenda/"
        assert halted(request).status_code == 503
        health = HttpRequest()
        health.path_info = "/healthz"
        assert halted(health).status_code == 200

        from apps.comms.tasks import (  # noqa: PLC0415 - deferred; heavy import
            dispatch_due_reminders,
            execute_operation,
        )

        with pytest.raises(activation.LiveModeHaltedError):
            execute_operation(operation_id="00000000-0000-0000-0000-000000000001")
        with pytest.raises(activation.LiveModeHaltedError):
            dispatch_due_reminders()


def test_require_data_mode_contract() -> None:
    assert require_data_mode("synthetic") == "synthetic"
    with pytest.raises(ImproperlyConfigured, match="CLINIC_DATA_MODE"):
        require_data_mode("bogus")
    with pytest.raises(ImproperlyConfigured, match="live data mode is not approved"):
        require_data_mode("live", {})
