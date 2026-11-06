"""Real broker/PG SIGKILL variant; never executed in the source-only lane."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.billing.psp_models import PspEvidence
from apps.billing.psp_services import prepare_psp_charge
from apps.comms.models import IntegrationOperation
from apps.core import integration
from celery import Celery

from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from billing.psp_broker_support import GateBroker, broker, event_process
from billing.test_psp_reconciliation import sandbox

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.environ.get("CLINIC_BROKER_GATE") != "required",
        reason="requires the dedicated real broker/PG gate",
    ),
]
__all__ = ("broker", "finance_setup")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _bind_real_broker(broker: GateBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the environment that Celery prioritizes over constructor settings."""
    monkeypatch.setenv("CELERY_BROKER_URL", broker.url)
    monkeypatch.setenv("CELERY_RESULT_BACKEND", broker.url)


@pytest.fixture
def prepared_psp(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FinanceSetup, UUID, UUID]:
    sandbox(finance_setup, settings)
    monkeypatch.setattr(integration, "_dispatch", lambda operation_id: None)
    terms = invoice_terms(finance_setup)
    with finance_scope(finance_setup):
        intent = prepare_psp_charge(
            clinic_id=finance_setup.clinic_id, invoice_id=terms.invoice_id, method="pix"
        )
        operation_id = IntegrationOperation.objects.get(subject_id=intent.pk).pk
    return finance_setup, intent.pk, operation_id


def test_required_broker_redelivers_after_remote_effect_sigkill_without_duplicate(
    prepared_psp: tuple[FinanceSetup, UUID, UUID],
    broker: GateBroker,
    app_database_url: str,
    synthetic_secret_backend: Path,
    tmp_path: Path,
) -> None:
    finance_setup, intent_id, operation_id = prepared_psp
    environment = {
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "MIGRATION_DATABASE_URL": app_database_url,
        "CELERY_BROKER_URL": broker.url,
        "CELERY_RESULT_BACKEND": broker.url,
        "CLINIC_SECRET_BACKEND": "synthetic-file",
        "CLINIC_SECRET_DIR": str(synthetic_secret_backend),
        "CLINIC_DATA_MODE": "synthetic",
        "BILLING_SYNTHETIC_PSP": "1",
        "CLINIC_BROKER_GATE": "required",
        "CLINIC_PSP_GATE_ROOT": str(tmp_path),
        "SECRET_KEY": "synthetic-worker-gate",
        "PYTHONPATH": str(ROOT / "tests"),
        "PATH": os.environ["PATH"],
    }
    argv = (
        sys.executable,
        "-m",
        "celery",
        "-A",
        "billing.psp_broker_app",
        "worker",
        "-Q",
        "clinic-integrations",
        "--concurrency=1",
        "-l",
        "INFO",
    )
    client = Celery("psp-gate-client", broker=broker.url, backend=broker.url)
    try:
        with event_process(
            argv, cwd=ROOT, environment=environment, ready_line="ready."
        ):
            completed = Event()

            def observed(*args: object, **kwargs: object) -> None:
                completed.set()

            task_id = uuid4().hex
            result = client.AsyncResult(task_id)
            # Register the Redis result subscription before the broker publish.
            result.then(observed)
            client.send_task(
                "comms.execute_operation",
                task_id=task_id,
                kwargs={"operation_id": str(operation_id)},
                queue="clinic-integrations",
            )
            assert result.get(timeout=90) == "succeeded"
            assert completed.wait(timeout=90)
        assert (tmp_path / "remote-create-attempts").read_text().splitlines() == [
            intent_id.hex
        ]
        with finance_scope(finance_setup):
            operation = IntegrationOperation.objects.get(pk=operation_id)
            assert operation.attempt_count == 1
            assert operation.status == "succeeded"
            assert (
                PspEvidence.objects.filter(
                    intent_id=intent_id, decision="verified"
                ).count()
                == 1
            )
    finally:
        client.close()
