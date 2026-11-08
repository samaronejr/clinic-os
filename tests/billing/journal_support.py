"""Real roles, genuine session proofs and synthetic financial fixtures."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.billing.collections import (
    allocate_settlement,
    observe_settlement,
    post_settlement,
    verify_settlement,
)
from apps.billing.compensation import create_payout_rule, prepare_invoice_terms
from apps.billing.journal import create_book
from apps.billing.money_types import (
    NO_ADJUSTMENTS,
    ChargeAdjustments,
    PayoutRuleTerms,
    SettlementObservation,
)
from apps.billing.services import create_invoice, issue_invoice
from apps.identity.models import UserClinicRole
from apps.scheduling.services import complete, start
from apps.tenancy.db import tenant_context

from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    AppointmentSetup,
    arrived_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apps.billing.models import (
        InvoiceTerms,
        JournalSettlement,
        SettlementAllocation,
    )

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True, slots=True)
class FinanceSetup:
    graph: RbacGraph
    appointment: AppointmentSetup
    book_id: UUID

    @property
    def clinic_id(self) -> UUID:
        return self.graph.clinic_a


@contextmanager
def finance_scope(setup: FinanceSetup) -> Iterator[None]:
    with (
        runtime_role(),
        tenant_context(setup.graph.clinic_admin, setup.graph.organization_a),
    ):
        yield


@pytest.fixture
def finance_setup(rbac_graph: RbacGraph) -> FinanceSetup:
    appointment = seed_appointment_setup(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        for actor, role in (
            (rbac_graph.shared_user, UserClinicRole.Role.OWNER),
            (rbac_graph.clinic_admin, UserClinicRole.Role.FINANCE),
        ):
            UserClinicRole.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                user_id=actor,
                role=role,
            )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        book = create_book(clinic_id=rbac_graph.clinic_a)
    return FinanceSetup(rbac_graph, appointment, book.pk)


def fresh_finance_session(setup: FinanceSetup) -> str:
    return session_key(verified_client(setup.graph))


def invoice_terms(
    setup: FinanceSetup,
    *,
    amount_minor: int = 10000,
    adjustments: ChargeAdjustments = NO_ADJUSTMENTS,
    payer: str = "patient",
    completed: bool = True,
) -> InvoiceTerms:
    graph = setup.graph
    with (
        runtime_role(),
        tenant_context(graph.shared_user, graph.organization_a),
    ):
        appointment = arrived_synthetic_appointment(setup.appointment)
        invoice = create_invoice(
            clinic_id=setup.clinic_id,
            patient_id=setup.appointment.patient_id,
            amount_minor=amount_minor,
            appointment_id=appointment.pk,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            expected_revision=invoice.revision,
        )
        rule = create_payout_rule(
            clinic_id=setup.clinic_id,
            practitioner_id=graph.physician,
            terms=PayoutRuleTerms("percent", 3000),
            command_id=uuid4(),
        )
    with finance_scope(setup):
        terms = prepare_invoice_terms(
            clinic_id=setup.clinic_id,
            invoice_id=invoice.pk,
            payer=payer,
            adjustments=adjustments,
            payout_rule_id=rule.pk,
        )
    if completed:
        with (
            runtime_role(),
            tenant_context(graph.physician, graph.organization_a),
        ):
            running = start(
                clinic_id=setup.clinic_id,
                appointment_id=appointment.pk,
                expected_revision=appointment.revision,
                command_id=uuid4(),
            )
            complete(
                clinic_id=setup.clinic_id,
                appointment_id=appointment.pk,
                expected_revision=running.revision,
                command_id=uuid4(),
            )
    return terms


def collected(
    setup: FinanceSetup, amount_minor: int, *, kind: str = "patient"
) -> JournalSettlement:
    receipt = observe_settlement(
        clinic_id=setup.clinic_id,
        observation=SettlementObservation(amount_minor, kind, "bank", uuid4()),
    )
    verify_settlement(clinic_id=setup.clinic_id, settlement_id=receipt.pk)
    return post_settlement(clinic_id=setup.clinic_id, settlement_id=receipt.pk)


def allocated(
    setup: FinanceSetup, terms: InvoiceTerms, amount_minor: int
) -> SettlementAllocation:
    settlement = collected(setup, amount_minor)
    return allocate_settlement(
        clinic_id=setup.clinic_id,
        settlement_id=settlement.pk,
        invoice_id=terms.invoice_id,
        amount_minor=amount_minor,
        command_id=uuid4(),
    )
