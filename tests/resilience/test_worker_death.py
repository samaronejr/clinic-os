"""Todo 72, scenario 14: a worker killed after the remote effect never repeats it.

Each case starts a real worker process against this test's database as
``clinic_app``. The worker performs the provider effect, makes it durable in a
ledger file and SIGKILLs itself before the local transition commits; recovery
then runs in a fresh process state.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.billing.psp_contracts import PaymentStatus
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_services import payment_request, prepare_psp_charge
from apps.billing.psp_worker import register_payment_provider
from apps.comms.models import IntegrationOperation
from apps.core import integration
from apps.core.integration import execute_operation, register_send_adapter
from apps.prescription import signing
from apps.prescription.models import SignatureCallback, SignatureOperation
from apps.prescription.signature_provider import SyntheticSignatureProvider
from psycopg.conninfo import conninfo_to_dict

from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from billing.test_psp_reconciliation import sandbox
from database_urls import database_url_for_name
from patient_service_support import runtime_role
from renewal.test_reminders import _book, _due, _operation
from renewal.test_reminders import setup as reminder_setup
from renewal.test_signatures import owner_scope
from renewal.test_signatures import setup as signing_setup
from resilience.death_worker import record_effect
from resilience.test_messaging_faults import CountingEmailAdapter

if TYPE_CHECKING:
    from uuid import UUID

    from apps.billing.psp_contracts import PaymentRequest
    from apps.prescription.signature_provider import (
        SignatureAcceptance,
        SignatureRequest,
    )
    from pytest_django.fixtures import SettingsWrapper

    from renewal.test_signatures import SigningSetup
    from scheduling.appointment_service_support import AppointmentSetup

__all__ = ("finance_setup", "reminder_setup", "signing_setup")
pytestmark = pytest.mark.django_db(transaction=True)
ROOT = Path(__file__).resolve().parents[2]
WORKER = (
    "import django; django.setup(); from resilience.death_worker import main; main()"
)
WORKER_TIMEOUT_SECONDS = 120


def die_after_effect(
    family: str,
    operation_id: UUID,
    ledger: Path,
    *,
    database_url: str,
    secret_root: Path,
) -> subprocess.CompletedProcess[str]:
    """Run one worker step that dies right after its provider effect."""
    environment = {
        "APP_DATABASE_URL": database_url,
        "BILLING_SYNTHETIC_PSP": "1",
        "CELERY_BROKER_URL": "memory://",
        "CLINIC_DATA_MODE": "synthetic",
        "CLINIC_SECRET_BACKEND": "synthetic-file",
        "CLINIC_SECRET_DIR": str(secret_root),
        "COMMS_SYNTHETIC_CHANNELS": "email",
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "MIGRATION_DATABASE_URL": database_url,
        "LOCKS_DATABASE_URL": database_url_for_name(
            os.environ["LOCKS_DATABASE_URL"],
            str(conninfo_to_dict(database_url)["dbname"]),
        ),
        "PATH": "/usr/bin:/bin",
        "PHYSICIAN_SYNTHETIC_REGISTRY": "1",
        "PRESCRIPTION_SYNTHETIC_SIGNING": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": f"{ROOT}{os.pathsep}{ROOT / 'tests'}",
        "PYTHONTZPATH": "",
        "TZ": "UTC",
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter and argv, no shell
        [sys.executable, "-c", WORKER, family, str(operation_id), str(ledger)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=WORKER_TIMEOUT_SECONDS,
        check=False,
    )


def effects(ledger: Path) -> list[str]:
    return ledger.read_text().splitlines() if ledger.exists() else []


def await_released(database_url: str, key: str) -> None:
    """Block until the killed worker's backend has released its session lock.

    The dead worker's backend exits on its own once it sees the closed socket;
    PostgreSQL wakes this blocked waiter at that moment, so recovery never
    races it. ``lock_timeout`` only bounds a backend that never exits.
    """
    with psycopg.connect(database_url, autocommit=True) as raw:
        raw.execute("SET lock_timeout = '30s'")
        raw.execute(
            "SELECT pg_catalog.pg_advisory_lock(pg_catalog.hashtextextended(%s, 0))",
            [key],
        )
        raw.execute(
            "SELECT pg_catalog.pg_advisory_unlock(pg_catalog.hashtextextended(%s, 0))",
            [key],
        )


def test_a_killed_messaging_worker_reconciles_without_resending(  # noqa: PLR0913
    reminder_setup: AppointmentSetup,
    app_database_url: str,
    superuser_database_url: str,
    synthetic_secret_backend: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup = reminder_setup
    _, operation_id = _book(setup, "email")
    ledger = tmp_path / "effects.log"
    worker = die_after_effect(
        "messaging",
        operation_id,
        ledger,
        database_url=app_database_url,
        secret_root=synthetic_secret_backend,
    )
    assert worker.returncode == -signal.SIGKILL, worker.stderr[-2000:]
    assert effects(ledger) == [f"messaging:synthetic:email:{operation_id}"]
    held = _operation(setup, operation_id)
    assert (held.status, held.provider_reference, held.attempt_count) == (
        "in_progress",
        None,
        1,
    )
    # Recovery: the next delivery sees the abandoned send and never re-sends.
    await_released(
        superuser_database_url, f"{integration._CLAIM_LOCK_NAMESPACE}{operation_id}"
    )
    adapter = CountingEmailAdapter()
    register_send_adapter(adapter)
    _due(setup, operation_id, monkeypatch)
    with runtime_role():
        assert execute_operation(operation_id) == "reconcile"
    assert adapter.sends == 0
    assert len(effects(ledger)) == 1
    assert _operation(setup, operation_id).status == "in_progress"


class RecordingSigner(SyntheticSignatureProvider):
    """The synthetic signer, keeping every recovery request it receives."""

    def __init__(self, ledger: Path) -> None:
        self.ledger = ledger
        self.requests: list[SignatureRequest] = []

    def begin(self, request: SignatureRequest) -> SignatureAcceptance:
        self.requests.append(request)
        acceptance = super().begin(request)
        record_effect(
            self.ledger, f"signing:{request.operation_id}:{acceptance.operation_ref}"
        )
        return acceptance


def test_a_killed_signing_worker_binds_one_operation_and_issues_once(  # noqa: PLR0913
    signing_setup: SigningSetup,
    app_database_url: str,
    superuser_database_url: str,
    synthetic_secret_backend: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    setup = signing_setup
    operation = setup.initiate()
    setup.prepare(operation.pk)
    prepared = setup.refresh(operation)
    assert prepared.state == "prepared"
    ledger = tmp_path / "effects.log"
    worker = die_after_effect(
        "signing",
        operation.pk,
        ledger,
        database_url=app_database_url,
        secret_root=synthetic_secret_backend,
    )
    assert worker.returncode == -signal.SIGKILL, worker.stderr[-2000:]
    [effect] = effects(ledger)
    accepted_reference = effect.rsplit(":", 1)[1]
    assert effect.startswith(f"signing:{operation.pk}:")
    killed = setup.refresh(operation)
    assert (killed.state, killed.operation_id) == ("prepared", "")
    # Both worker receipts must identify the same provider operation.
    await_released(
        superuser_database_url, f"{signing._CLAIM_LOCK_NAMESPACE}{operation.pk}"
    )
    signer = RecordingSigner(ledger)
    monkeypatch.setattr(signing, "signature_provider_for", lambda _name: signer)
    assert setup.dispatch(operation.pk) == "signing"
    [retry] = signer.requests
    assert (retry.operation_id, retry.content_digest) == (
        operation.pk,
        killed.content_digest,
    )
    bound = setup.refresh(operation)
    assert bound.operation_id == accepted_reference
    assert effects(ledger) == [effect, effect]
    assert len({line.rsplit(":", 1)[1] for line in effects(ledger)}) == 1
    callback = setup.callback(bound)
    assert setup.receive(*callback) == "applied"
    assert setup.receive(*callback) == "duplicate"
    assert setup.refresh(operation).state == "rehearsal_complete"
    with owner_scope(setup.graph.organization_a):
        assert SignatureOperation.objects.count() == 1
        assert SignatureCallback.objects.count() == 1


class LookupOnlyProvider(RecordedPaymentProvider):
    """The provider after the crash: it holds the charge and counts creates."""

    def __init__(self, known: dict[UUID, PaymentStatus]) -> None:
        super().__init__(secret=b"synthetic-fixture", records=known)
        self.creates = 0

    def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
        self.creates += 1
        return super().create_charge(request=request)


def test_a_killed_payment_worker_recovers_by_lookup_without_a_second_charge(  # noqa: PLR0913
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    app_database_url: str,
    superuser_database_url: str,
    synthetic_secret_backend: Path,
    tmp_path: Path,
) -> None:
    sandbox(finance_setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    terms = invoice_terms(finance_setup)
    register_payment_provider(
        RecordedPaymentProvider(secret=b"synthetic-fixture", records={})
    )
    with finance_scope(finance_setup):
        intent = prepare_psp_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=terms.invoice_id, method="pix"
        )
        request = payment_request(intent)
        operation = IntegrationOperation.objects.get(subject_id=intent.pk)
    ledger = tmp_path / "effects.log"
    worker = die_after_effect(
        "payment",
        operation.pk,
        ledger,
        database_url=app_database_url,
        secret_root=synthetic_secret_backend,
    )
    assert worker.returncode == -signal.SIGKILL, worker.stderr[-2000:]
    [effect] = effects(ledger)
    reference = effect.rsplit(":", 1)[1]
    with finance_scope(finance_setup):
        operation.refresh_from_db()
        assert (operation.status, operation.attempt_count) == ("in_progress", 1)
    # Recovery: the provider now reports the charge; recovery only looks it up.
    await_released(
        superuser_database_url, f"{integration._CLAIM_LOCK_NAMESPACE}{operation.pk}"
    )
    client = LookupOnlyProvider(
        {
            request.idempotency_key: PaymentStatus(
                reference, "pending", request.amount_minor, "BRL", 1
            )
        }
    )
    register_payment_provider(client)
    with runtime_role():
        assert integration.execute_operation(operation.pk) == "succeeded"
    assert client.creates == 0
    assert len(effects(ledger)) == 1
    with finance_scope(finance_setup):
        operation.refresh_from_db()
        assert (operation.status, operation.provider_reference) == (
            "succeeded",
            reference,
        )
