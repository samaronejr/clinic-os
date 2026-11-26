"""Remote acceptance is not local completion until the DB accepts the version."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import TYPE_CHECKING

import pytest
from apps.audit.models import AuditEvent
from apps.billing import psp_worker
from apps.billing.psp_fixtures import RecordedPaymentProvider
from apps.billing.psp_models import PspEvidence, PspReceipt
from apps.billing.psp_reconciliation import reconcile_psp_intent
from apps.billing.psp_storage import record_status
from apps.billing.psp_worker import register_payment_provider
from apps.comms.models import IntegrationOperation
from apps.core import integration
from django.db import connections

from billing.journal_support import FinanceSetup, finance_scope, finance_setup
from billing.test_psp_adapter import PAID, SECRET
from billing.test_psp_review import prepared_payment
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.billing.psp_contracts import PaymentRequest, PaymentStatus
    from apps.billing.psp_models import PspIntent

__all__ = ("finance_setup", "prepared_payment")
pytestmark = pytest.mark.django_db(transaction=True)


class LateAcceptance(RecordedPaymentProvider):
    """Hold the original reply after the remote effect, not inside a DB transaction."""

    def __init__(self, cut: Barrier | None) -> None:
        super().__init__(secret=SECRET, records={})
        self.cut = cut
        self.creates = 0

    def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
        self.creates += 1
        if self.cut is not None:
            self.cut.wait()
            self.cut.wait()
        return PAID

    def lookup(self, *, request: PaymentRequest) -> PaymentStatus | None:
        if not self.creates:
            return None
        return replace(PAID, status="reversed", version=3)


def _pause_after[**P, T](call: Callable[P, T], cut: Barrier) -> Callable[P, T]:
    def paused(*args: P.args, **kwargs: P.kwargs) -> T:
        result = call(*args, **kwargs)
        cut.wait()
        cut.wait()
        return result

    return paused


@pytest.mark.parametrize(
    "after_retention", [False, True], ids=["before-retain", "after-retain"]
)
def test_first_send_never_completes_against_a_committed_newer_version(
    prepared_payment: tuple[FinanceSetup, PspIntent],
    monkeypatch: pytest.MonkeyPatch,
    after_retention: bool,
) -> None:
    setup, intent = prepared_payment
    cut = Barrier(2)
    provider = LateAcceptance(None if after_retention else cut)
    if after_retention:
        # Observe the real database return; no version or decision is mocked.
        monkeypatch.setattr(
            psp_worker, "record_status", _pause_after(record_status, cut)
        )
    register_payment_provider(provider)
    with finance_scope(setup):
        operation = IntegrationOperation.objects.get(subject_id=intent.pk)

    def send() -> str:
        try:
            with runtime_role():
                return integration.execute_operation(operation.pk)
        except BaseException:
            cut.abort()
            raise
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(send)
        try:
            cut.wait()
            with runtime_role():
                assert reconcile_psp_intent(intent_id=intent.pk) == "quarantined"
            cut.wait()
        except BaseException:
            cut.abort()
            raise
        outcome = pending.result()
    with finance_scope(setup):
        operation.refresh_from_db()
        assert list(
            PspEvidence.objects.filter(intent=intent)
            .order_by("version")
            .values_list("version", "status", "decision")
        ) == [
            (2, "settled", "verified" if after_retention else "superseded"),
            (3, "reversed", "quarantined"),
        ]
        assert (outcome, operation.status, operation.provider_reference) == (
            "reconcile",
            "in_progress",
            None,
        )
        assert provider.creates == operation.attempt_count == 1
        assert not PspReceipt.objects.filter(intent=intent).exists()
    with owner_context(intent.organization_id):
        assert not AuditEvent.objects.filter(
            event_type="comms.operation.succeeded", affected_record_id=str(operation.pk)
        ).exists()
