from __future__ import annotations

import importlib
import socket
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest
from config.settings.browser_rpc import receive_reserved_attestation
from django.core.exceptions import ImproperlyConfigured
from ops.testing.isolation_common import JsonObject, canonical_bytes

from browser.browser_settings_claim_builders import active_process_ledger
from browser.browser_settings_claim_values import DATABASE_ID, PROCESS_ID
from browser.browser_settings_fixtures import browser_ledger_fixture
from browser.browser_settings_test_vectors import (
    browser_environment,
    environment_drift,
    response,
)
from browser.browser_settings_test_vectors import (
    drift as apply_drift,
)

_run_process = subprocess.run


@runtime_checkable
class _LedgerValidator(Protocol):
    def __call__(
        self, ledger: JsonObject, expected: JsonObject, phase: str
    ) -> None: ...


@runtime_checkable
class _LiveAttestor(Protocol):
    def __call__(
        self, ledger_path: Path, expected: JsonObject, phase: str
    ) -> JsonObject: ...


@runtime_checkable
class _EnvironmentValidator(Protocol):
    def __call__(self, environment: dict[str, str]) -> JsonObject: ...


@runtime_checkable
class _ResponseValidator(Protocol):
    def __call__(
        self,
        response: JsonObject,
        *,
        attempt_id: str,
        claim_id: str,
        status: str,
        sequence: int,
    ) -> JsonObject: ...


def _function(module_name: str, name: str, contract: type[object]) -> object:
    module = importlib.import_module(module_name)
    candidate = getattr(module, name, None)
    assert isinstance(candidate, contract)
    return candidate


def test_browser_authority_accepts_reserved_import_then_active_runtime(
    tmp_path: Path,
) -> None:
    ledger_path, ledger, expected = browser_ledger_fixture(tmp_path)
    validator = _function(
        "config.settings.browser_authority", "validate_browser_ledger", _LedgerValidator
    )
    assert isinstance(validator, _LedgerValidator)
    validator(ledger, expected, "reserved")
    validator(active_process_ledger(ledger), expected, "active")

    attestor = _function(
        "config.settings.browser_authority", "attest_browser_authority", _LiveAttestor
    )
    assert isinstance(attestor, _LiveAttestor)
    response = attestor(ledger_path, expected, "reserved")
    assert response["claim_status"] == "reserved"


@pytest.mark.parametrize(
    "drift",
    [
        "wrong-purpose",
        "stale",
        "baseline-collision",
        "baseline-network-collision",
        "reserved-dependency",
        "uid",
        "pid",
        "network",
        "listener",
        "environment",
        "ca-file",
        "volume-identity",
        "unknown-schema-key",
    ],
)
def test_browser_authority_rejects_each_drift_class(tmp_path: Path, drift: str) -> None:
    _, ledger, expected = browser_ledger_fixture(tmp_path)
    phase = "active" if drift == "pid" else "reserved"
    candidate = active_process_ledger(ledger) if phase == "active" else ledger
    apply_drift(candidate, drift)
    validator = _function(
        "config.settings.browser_authority", "validate_browser_ledger", _LedgerValidator
    )
    assert isinstance(validator, _LedgerValidator)
    with pytest.raises(ImproperlyConfigured, match="browser authority"):
        validator(candidate, expected, phase)


@pytest.mark.parametrize(
    "drift",
    [
        "unknown-key",
        "wrong-host",
        "shared-project",
        "shared-database",
        "fixed-secret",
        "owner-role",
        "permissive-tls",
        "timeout-four",
        "live-mode",
    ],
)
def test_browser_environment_is_closed_synthetic_and_claim_bound(
    tmp_path: Path, drift: str
) -> None:
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)) as server:
        socket_path = tmp_path / "ledger-rpc.sock"
        server.bind(str(socket_path))
        socket_path.chmod(0o600)
        environment = browser_environment(tmp_path, socket_path)
        environment_drift(environment, drift)
        validator = _function(
            "config.settings.browser_contract",
            "validate_browser_environment",
            _EnvironmentValidator,
        )
        assert isinstance(validator, _EnvironmentValidator)
        with pytest.raises(ImproperlyConfigured, match="browser settings"):
            validator(environment)


def test_browser_response_is_closed_fresh_and_identity_bound() -> None:
    validator = _function(
        "config.settings.browser_rpc",
        "validate_attestation_response",
        _ResponseValidator,
    )
    assert isinstance(validator, _ResponseValidator)
    candidate = response()
    validated = validator(
        candidate,
        attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        claim_id=PROCESS_ID,
        status="reserved",
        sequence=1,
    )
    assert validated == candidate
    for field, value in (
        ("claim_id", DATABASE_ID),
        ("claim_status", "active"),
        ("sequence", 2),
        ("verified_at_utc", "2000-01-01T00:00:00.000000Z"),
    ):
        drifted = candidate.copy()
        drifted[field] = value
        with pytest.raises(ImproperlyConfigured, match="browser attestation"):
            validator(
                drifted,
                attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                claim_id=PROCESS_ID,
                status="reserved",
                sequence=1,
            )


def test_browser_rpc_receives_one_canonical_reserved_attestation(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "ledger-rpc.sock"
    candidate = response()
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)) as server:
        server.bind(str(socket_path))
        socket_path.chmod(0o600)
        server.listen(1)

        def publish() -> None:
            connection, _ = server.accept()
            with connection:
                connection.sendall(canonical_bytes(candidate))

        publisher = threading.Thread(target=publish)
        publisher.start()
        received = receive_reserved_attestation(
            socket_path,
            attempt_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            claim_id=PROCESS_ID,
        )
        publisher.join(timeout=1)
        assert not publisher.is_alive()
    assert received == candidate


def test_browser_settings_import_uses_only_attested_app_runtime(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "ledger-rpc.sock"
    environment = browser_environment(tmp_path, socket_path)
    candidate = response()
    probe = (
        "import config.settings.browser as s;"
        "print(s.CLINIC_PROCESS_ROLE, s.DATABASES['default']['NAME'],"
        "s.ALLOWED_HOSTS[0], s.SECURE_PROXY_SSL_HEADER, s.DEBUG, sep='|')"
    )
    with closing(socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)) as server:
        server.bind(str(socket_path))
        socket_path.chmod(0o600)
        server.listen(1)

        def publish() -> None:
            connection, _ = server.accept()
            with connection:
                connection.sendall(canonical_bytes(candidate))

        publisher = threading.Thread(target=publish)
        publisher.start()
        completed = _run_process(
            [sys.executable, "-c", probe],
            cwd=Path.cwd(),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        publisher.join(timeout=1)
        assert not publisher.is_alive()
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == (
        "clinic_app|clinic_phase1a_browser_fixture_clinic|"
        "phase1a-browser.qa.clinic-os.test|None|False\n"
    )
