"""F7: Redis redelivers the original task after durable provider acceptance."""

from __future__ import annotations

import json
import os
import signal
from contextlib import contextmanager
from hashlib import sha256
from typing import TYPE_CHECKING

import pytest
from apps.comms.tasks import dispatch_due_reminders
from apps.prescription.models import (
    PrescriptionDocument,
    SignatureCallback,
    SignatureOperation,
)
from apps.prescription.signing import request_signature
from apps.tenancy.db import tenant_context
from django.db import connection

from patient_service_support import runtime_role
from renewal.broker_support import (
    QUEUE,
    broker,
    broker_url,
    delivery,
    read_event,
    worker,
)
from renewal.test_broker_delivery import _operation, book_reminder, make_due
from renewal.test_broker_delivery import setup as messaging_setup
from renewal.test_signatures import owner_scope
from renewal.test_signatures import setup as signing_setup

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from redis import Redis

    from renewal.broker_support import Worker
    from renewal.test_signatures import SigningSetup
    from scheduling.appointment_service_support import AppointmentSetup

__all__ = ("broker", "broker_url", "messaging_setup", "signing_setup", "worker")
pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.environ.get("CLINIC_BROKER_GATE") != "required",
        reason="requires the dedicated real broker/PG gate",
    ),
]


@contextmanager
def _accepted_before_sigkill(redis: Redis, worker_pid: int) -> Iterator[str]:
    pid, task_id = read_event(redis, "accepted")
    assert isinstance(pid, int)
    assert os.getpgid(pid) == worker_pid
    assert pid != worker_pid
    assert isinstance(task_id, str)
    unacked_ids = {
        json.loads(value)[0]["headers"]["id"] for value in redis.hvals("unacked")
    }
    try:
        yield task_id
    finally:
        os.kill(pid, signal.SIGKILL)
    assert task_id in unacked_ids, "task was acknowledged before local completion"


def test_messaging_redelivery_after_provider_acceptance(
    messaging_setup: AppointmentSetup, broker: Redis, worker: Worker, tmp_path: Path
) -> None:
    setup = messaging_setup
    operation_id = book_reminder(setup, "sms")
    make_due(setup, operation_id)
    ledger = tmp_path / "remote-effects"
    with worker.start(CLINIC_GATE_EFFECT_LEDGER=str(ledger)) as process:
        with runtime_role():
            assert dispatch_due_reminders() == 1
        with _accepted_before_sigkill(broker, process.pid) as task_id:
            assert ledger.read_text().splitlines() == [f"synthetic:sms:{operation_id}"]
            held = _operation(setup.organization_id, operation_id)
            assert (held.status, held.provider_reference, held.attempt_count) == (
                "in_progress",
                None,
                1,
            )
        completed = delivery(broker, operation_id)
    assert (completed.task_id, completed.redelivered) == (task_id, True)
    assert ledger.read_text().splitlines() == [f"synthetic:sms:{operation_id}"], (
        "messaging redelivery repeated the provider effect"
    )
    assert completed.outcome == "reconcile"
    # Without an authoritative lookup the safe final state retains ambiguity.
    # The second claim reconciles; it neither invokes send nor invents a receipt.
    operation = _operation(setup.organization_id, operation_id)
    assert (
        operation.status,
        operation.provider_reference,
        operation.attempt_count,
    ) == (
        "in_progress",
        None,
        2,
    )


def test_signing_redelivery_after_provider_acceptance(
    signing_setup: SigningSetup, broker: Redis, worker: Worker, tmp_path: Path
) -> None:
    setup = signing_setup
    ledger = tmp_path / "remote-effects"
    with (
        runtime_role(),
        tenant_context(setup.graph.physician, setup.graph.organization_a),
    ):
        operation = request_signature(
            request=setup.request,
            clinic_id=setup.graph.clinic_a,
            document_id=setup.document.pk,
        )
        assert operation.state == "prepared"
        assert broker.llen(QUEUE) == 0
    assert broker.llen(QUEUE) == 1
    with worker.start(CLINIC_GATE_EFFECT_LEDGER=str(ledger)) as process:
        with _accepted_before_sigkill(broker, process.pid) as task_id:
            accepted_effects = ledger.read_text().splitlines()
            assert len(accepted_effects) == 1
            held = setup.refresh(operation)
            assert (held.state, held.operation_id, held.signed_bytes) == (
                "prepared",
                "",
                None,
            )
            with runtime_role(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT activity.application_name "
                    "FROM pg_catalog.pg_locks AS held "
                    "JOIN pg_catalog.pg_stat_activity AS activity USING (pid) "
                    "WHERE held.locktype = 'advisory' AND held.granted "
                    "AND activity.datname = pg_catalog.current_database() "
                    "AND activity.application_name "
                    "IN ('f7-gate-default', 'f7-gate-locks')"
                )
                assert cursor.fetchall() == [("f7-gate-locks",)], (
                    "signing advisory lock was not held on the locks alias"
                )
        completed_delivery = delivery(broker, operation.pk)
    assert (completed_delivery.task_id, completed_delivery.redelivered) == (
        task_id,
        True,
    )
    assert ledger.read_text().splitlines() == accepted_effects, (
        "signing redelivery repeated the provider effect"
    )
    accepted = f"synop-{operation.pk.hex}"
    assert accepted_effects == [f"{operation.pk} {accepted}"]
    assert ledger.with_suffix(".lookups").read_text().splitlines() == accepted_effects
    assert completed_delivery.outcome == "signing"
    bound = setup.refresh(operation)
    assert (bound.state, bound.operation_id) == ("signing", accepted)
    callback = setup.callback(bound)
    assert setup.receive(*callback) == "applied"
    assert setup.receive(*callback) == "duplicate"
    completed = setup.refresh(operation)
    assert completed.state == "rehearsal_complete"
    signed_bytes = bytes(completed.signed_bytes)
    assert signed_bytes.startswith(bytes(setup.document.pdf_bytes))
    assert completed.signed_digest == sha256(signed_bytes).hexdigest()
    with owner_scope(setup.graph.organization_a):
        assert list(
            PrescriptionDocument.objects.filter(
                draft_id=setup.document.draft_id
            ).values_list("pk", flat=True)
        ) == [setup.document.pk]
        assert SignatureOperation.objects.filter(document=setup.document).count() == 1
        assert SignatureCallback.objects.filter(operation=operation).count() == 1
