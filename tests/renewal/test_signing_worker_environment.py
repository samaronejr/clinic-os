"""Non-browser proof of the signing worker's process boundary."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

from renewal.browser import test_document_verification as verification

if TYPE_CHECKING:
    import pytest
    from playwright.sync_api import Page


def test_signing_worker_receives_only_runtime_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    privileged_inputs = {
        "CLINIC_RENEWAL_FIXTURE_DATABASE_URL": "fake-fixture-owner",
        "MIGRATION_DATABASE_URL": "fake-migration-owner",
        "TEST_SUPERUSER_DATABASE_URL": "fake-superuser",
        "LOCKS_DATABASE_URL": "fake-alternate-database",
        "PGPASSWORD": "fake-libpq-password",
        "CLINIC_RENEWAL_PASSWORD": "fake-staff-password",
        "SECRET_KEY": "fake-server-secret",
    }
    for key, value in privileged_inputs.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("CLINIC_RENEWAL_WORKER_DATABASE_URL", "fake-runtime-app")
    backend, key_directory = "synthetic-file", "/synthetic-fixture/keys"
    monkeypatch.setenv("CLINIC_SECRET_BACKEND", backend)
    monkeypatch.setenv("CLINIC_SECRET_DIR", key_directory)

    operation = "00000000-0000-0000-0000-000000000001"
    monkeypatch.setattr(verification, "expect", Mock())
    monkeypatch.setattr(verification, "reload_settled", Mock())
    monkeypatch.setattr(
        verification,
        "_operation_row",
        Mock(return_value={"id": operation, "state": "prepared", "operation_ref": ""}),
    )
    launch = Mock(return_value=subprocess.CompletedProcess([], 0, "retry\n", ""))
    monkeypatch.setattr(verification, "subprocess", Mock(run=launch))

    outcome = verification.drive_signature(
        cast("Page", Mock()), {}, "fixture", fault="down"
    )
    assert outcome == "retry"

    launch.assert_called_once()
    args, kwargs = launch.call_args
    assert args[0][-1] == operation
    env = kwargs["env"]
    assert env["APP_DATABASE_URL"] == "fake-runtime-app"
    inherited_keys = privileged_inputs.keys() & env.keys()
    assert inherited_keys == set()
    assert set(privileged_inputs.values()).isdisjoint(env.values())
    assert "CLINIC_RENEWAL_WORKER_DATABASE_URL" not in env
    assert env["CLINIC_SECRET_BACKEND"] == backend
    assert env["CLINIC_SECRET_DIR"] == key_directory
    assert env["CLINIC_SYNTHETIC_FAULTS"] == "signing=down"
