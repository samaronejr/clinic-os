"""Legacy charge replay retains the requested clinic before inspecting rows."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Never
from uuid import UUID

import pytest
from apps.billing import services
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.models import Invoice
from apps.identity.models import Clinic

from billing.replay_support import QueryTrace, VisibleRows, refusal_shape


@dataclass
class ChargeScope:
    clinic: Clinic
    invoice: Invoice
    trace: QueryTrace


@pytest.fixture
def charge_scope(monkeypatch: pytest.MonkeyPatch) -> ChargeScope:
    invoice = Invoice(
        id=UUID(int=5830),
        organization_id=UUID(int=5831),
        clinic_id=UUID(int=5832),
        patient_id=UUID(int=5833),
        idempotency_key=UUID(int=5834),
        amount_minor=100,
        create_fingerprint=services._charge_fingerprint(
            UUID(int=5832), UUID(int=5833), 100
        ),
    )
    scope = ChargeScope(
        Clinic(id=UUID(int=5835), organization_id=invoice.organization_id),
        invoice,
        [],
    )

    def authorized_clinic(
        clinic_id: UUID, *, permission: str = "charge.create"
    ) -> tuple[UUID, Clinic]:
        assert clinic_id == scope.clinic.pk
        return UUID(int=5836), scope.clinic

    def unknown_patient(**selectors: object) -> Never:
        scope.trace.append(("patient.require", tuple(sorted(selectors))))
        msg = "billing access denied"
        raise BillingAccessDeniedError(msg)

    monkeypatch.setattr(services, "_clinic", authorized_clinic)
    monkeypatch.setattr(services, "require_survivor", unknown_patient)
    monkeypatch.setattr("apps.billing.services.transaction.atomic", nullcontext)
    monkeypatch.setattr(
        Invoice.objects,
        "filter",
        VisibleRows((invoice,), scope.trace, "invoice").filter,
    )
    return scope


def test_foreign_charge_operation_is_indistinguishable_from_unknown(
    charge_scope: ChargeScope,
) -> None:
    traces = []
    for key in (charge_scope.invoice.idempotency_key, UUID(int=5837)):
        charge_scope.trace.clear()
        assert (
            services.charge_for_operation(
                clinic_id=charge_scope.clinic.pk, idempotency_key=key
            )
            is None
        )
        traces.append(tuple(charge_scope.trace))
    assert traces[0] == traces[1]


def test_foreign_create_key_follows_the_same_denial_path_as_unknown(
    charge_scope: ChargeScope,
) -> None:
    denials = []
    traces = []
    for key in (charge_scope.invoice.idempotency_key, UUID(int=5837)):
        charge_scope.trace.clear()
        with pytest.raises(BillingAccessDeniedError) as denied:
            services.create_invoice(
                clinic_id=charge_scope.clinic.pk,
                patient_id=charge_scope.invoice.patient_id,
                amount_minor=100,
                idempotency_key=key,
            )
        denials.append(refusal_shape(denied.value))
        traces.append(tuple(charge_scope.trace))
    assert denials[0] == denials[1]
    assert traces[0] == traces[1]


def test_same_scope_charge_creation_and_projection_replay_the_original(
    charge_scope: ChargeScope,
) -> None:
    original = charge_scope.invoice
    charge_scope.clinic.pk = original.clinic_id
    assert (
        services.create_invoice(
            clinic_id=charge_scope.clinic.pk,
            patient_id=original.patient_id,
            amount_minor=100,
            idempotency_key=original.idempotency_key,
        )
        is original
    )
    assert (
        services.charge_for_operation(
            clinic_id=charge_scope.clinic.pk, idempotency_key=original.idempotency_key
        )
        is original
    )
