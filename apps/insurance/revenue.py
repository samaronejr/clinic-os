"""Retained payer returns, clinician-bound appeals and independent cash allocation."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, cast
from uuid import uuid5

from django.db import transaction
from django.db.models import Sum

from apps.billing.collections import allocate_settlement
from apps.billing.models import JournalSettlement
from apps.insurance.access import (
    audit,
    authority,
    clinic_write,
    fingerprint,
    replay,
    scoped,
)
from apps.insurance.batches import require_rehearsal
from apps.insurance.errors import (
    InsuranceConflictError,
    InsuranceDeniedError,
    InsuranceInputError,
)
from apps.insurance.guides import stored_facts
from apps.insurance.limits import MAX_APPEAL_TEXT
from apps.insurance.models import (
    Appeal,
    Batch,
    ClinicalApproval,
    Glosa,
    Guide,
    InsuranceMembership,
    PayerContract,
    PayerRequest,
    PaymentMatch,
    Return,
)
from apps.insurance.tiss import parse_return
from apps.insurance.transitions import transition
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_locking import acquire_appointment_write_gates
from apps.scheduling.appointment_transition_state import (
    reload_transition_appointment,
    transition_write_target,
)
from apps.scheduling.models import Appointment
from apps.workflows.services import TaskOwner, TaskSpec, assign_task, create_task

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class PayerCheck:
    """A bounded synthetic eligibility/authorization check with accountable due time."""

    appointment_id: UUID
    kind: str
    due_at: datetime
    fixture_outcome: str


def ingest_return(
    *, clinic_id: UUID, batch_id: UUID, xml: bytes, command_id: UUID
) -> Return:
    """Retain an XSD-valid return once; duplicated bytes cannot duplicate glosas."""
    require_rehearsal()
    checksum = sha256(xml).hexdigest()
    digest = fingerprint([str(batch_id), checksum])
    with clinic_write(clinic_id=clinic_id) as clinic:
        batch = scoped(model=Batch, clinic_id=clinic_id, identifier=batch_id)
        existing = replay(
            model=Return, clinic_id=clinic_id, command_id=command_id, digest=digest
        )
        duplicate = Return.objects.filter(
            clinic_id=clinic_id, batch=batch, source_sha256=checksum
        ).first()
        if existing is not None or duplicate is not None:
            return existing if existing is not None else cast("Return", duplicate)
        if batch.state not in {"acknowledged", "returned"}:
            raise InsuranceConflictError
        lines = parse_return(xml=xml, versions=cast("dict[str, str]", batch.versions))
        guides = {
            guide.number: guide
            for guide in Guide.objects.filter(
                clinic_id=clinic_id, id__in=batch.guide_ids
            )
        }
        for line in lines:
            guide = guides.get(line.guide_number)
            if guide is None:
                raise InsuranceDeniedError
            facts = stored_facts(guide)
            if not 1 <= line.sequence <= len(facts.procedures):
                raise InsuranceConflictError
            procedure = facts.procedures[line.sequence - 1]
            if (
                line.registry != facts.registry
                or line.batch_number != batch.number
                or line.card != facts.card
                or (line.table, line.code, line.quantity)
                != (procedure.table, procedure.code, procedure.quantity)
                or line.informed_minor != procedure.quantity * procedure.unit_minor
            ):
                raise InsuranceConflictError
        source = Return.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            batch=batch,
            source_sha256=checksum,
            xml=xml,
        )
        for line in lines:
            guide = guides[line.guide_number]
            Glosa.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                command_id=uuid5(command_id, f"{guide.pk}:{line.sequence}"),
                fingerprint=fingerprint(
                    [
                        checksum,
                        str(guide.pk),
                        line.sequence,
                        line.informed_minor,
                        line.accepted_minor,
                        line.denied_minor,
                        list(line.glosas),
                    ]
                ),
                source=source,
                guide=guide,
                sequence=line.sequence,
                charged_minor=line.informed_minor,
                accepted_minor=line.accepted_minor,
                denied_minor=line.denied_minor,
                code=line.glosas[0][0] if line.glosas else "",
                detail={"glosas": [list(item) for item in line.glosas]},
            )
        if batch.state == "acknowledged":
            transition(
                clinic=clinic,
                row=batch,
                target="returned",
                command_id=uuid5(command_id, "returned"),
            )
        audit(clinic_id=clinic_id, row=source, event="insurance.return.ingested")
        return source


def draft_appeal(
    *,
    clinic_id: UUID,
    glosa_id: UUID,
    body: str,
    clinical_assertions: str,
    command_id: UUID,
) -> Appeal:
    """Require a separate clinician approval for new assertions in these exact bytes."""
    if (
        not 1 <= len(body) <= MAX_APPEAL_TEXT
        or len(clinical_assertions) > MAX_APPEAL_TEXT
    ):
        raise InsuranceInputError
    digest = fingerprint([str(glosa_id), body, clinical_assertions])
    with clinic_write(clinic_id=clinic_id) as clinic:
        glosa = scoped(model=Glosa, clinic_id=clinic_id, identifier=glosa_id)
        if glosa.denied_minor == 0:
            raise InsuranceConflictError
        existing = replay(
            model=Appeal, clinic_id=clinic_id, command_id=command_id, digest=digest
        )
        if existing is not None:
            return existing
        row = Appeal.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            glosa=glosa,
            body=body,
            clinical_assertions=clinical_assertions,
            content_sha256=digest,
        )
        audit(clinic_id=clinic_id, row=row, event="insurance.appeal.draft")
        return row


def approve_appeal_assertions(
    *, clinic_id: UUID, appeal_id: UUID, command_id: UUID
) -> ClinicalApproval:
    """Require the dedicated clinic-scoped professional permission."""
    with clinic_write(
        clinic_id=clinic_id, permission="tiss.clinical_approve"
    ) as clinic:
        approver = authority(clinic_id=clinic_id, permission="tiss.clinical_approve")
        appeal = scoped(model=Appeal, clinic_id=clinic_id, identifier=appeal_id)
        actual = fingerprint(
            [str(appeal.glosa_id), appeal.body, appeal.clinical_assertions]
        )
        if actual != appeal.content_sha256:
            raise InsuranceConflictError
        digest = fingerprint([str(appeal.pk), actual, str(approver)])
        existing = replay(
            model=ClinicalApproval,
            clinic_id=clinic_id,
            command_id=command_id,
            digest=digest,
        )
        if existing is not None:
            return existing
        if appeal.state != "draft":
            raise InsuranceConflictError
        row = ClinicalApproval.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            appeal=appeal,
            approved_by_id=approver,
            content_sha256=actual,
        )
        audit(clinic_id=clinic_id, row=row, event="insurance.appeal.clinical_approved")
        return row


def approve_appeal(*, clinic_id: UUID, appeal_id: UUID, command_id: UUID) -> Appeal:
    """Finance approves sending; PostgreSQL rechecks any prior clinical approval."""
    with clinic_write(clinic_id=clinic_id) as clinic:
        row = scoped(model=Appeal, clinic_id=clinic_id, identifier=appeal_id)
        if (
            row.clinical_assertions
            and not ClinicalApproval.objects.filter(
                clinic_id=clinic_id, appeal=row, content_sha256=row.content_sha256
            ).exists()
        ):
            raise InsuranceConflictError
        transition(clinic=clinic, row=row, target="approved", command_id=command_id)
        return row


def submit_appeal(*, clinic_id: UUID, appeal_id: UUID, command_id: UUID) -> Appeal:
    """Retain the rehearsal receipt; no real operator leg is available."""
    require_rehearsal()
    with clinic_write(clinic_id=clinic_id) as clinic:
        row = scoped(model=Appeal, clinic_id=clinic_id, identifier=appeal_id)
        transition(clinic=clinic, row=row, target="submitted", command_id=command_id)
        return row


def reconcile_payment(
    *,
    clinic_id: UUID,
    guide_id: UUID,
    settlement_id: UUID,
    amount_minor: int,
    command_id: UUID,
) -> PaymentMatch:
    """Allocate independently posted insurer cash, never an adjudication total."""
    if type(amount_minor) is not int or amount_minor <= 0:
        raise InsuranceInputError
    digest = fingerprint([str(guide_id), str(settlement_id), amount_minor])
    with clinic_write(clinic_id=clinic_id) as clinic:
        guide = scoped(model=Guide, clinic_id=clinic_id, identifier=guide_id)
        existing = replay(
            model=PaymentMatch,
            clinic_id=clinic_id,
            command_id=command_id,
            digest=digest,
        )
        if existing is not None:
            return existing
        settlement = JournalSettlement.objects.filter(
            pk=settlement_id,
            book__clinic_id=clinic_id,
            kind="insurer",
            state="posted",
            entry_id__isnull=False,
        ).first()
        if settlement is None:
            raise InsuranceDeniedError
        latest = (
            Glosa.objects.filter(clinic_id=clinic_id, guide=guide)
            .order_by("-source__created_at", "-source_id")
            .first()
        )
        if latest is None:
            raise InsuranceConflictError
        accepted = (
            Glosa.objects.filter(
                clinic_id=clinic_id, guide=guide, source_id=latest.source_id
            ).aggregate(total=Sum("accepted_minor"))["total"]
            or 0
        )
        allocated = (
            PaymentMatch.objects.filter(
                clinic_id=clinic_id, guide__invoice_id=guide.invoice_id
            ).aggregate(total=Sum("amount_minor"))["total"]
            or 0
        )
        if amount_minor + allocated > accepted:
            raise InsuranceConflictError
        allocation = allocate_settlement(
            clinic_id=clinic_id,
            settlement_id=settlement_id,
            invoice_id=guide.invoice_id,
            amount_minor=amount_minor,
            command_id=uuid5(command_id, "payer-allocation"),
        )
        row = PaymentMatch.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            command_id=command_id,
            fingerprint=digest,
            guide=guide,
            settlement=settlement,
            allocation=allocation,
            amount_minor=amount_minor,
        )
        audit(clinic_id=clinic_id, row=row, event="insurance.payment.reconciled")
        return row


def request_payer_check(
    *,
    clinic_id: UUID,
    membership_id: UUID,
    contract_id: UUID,
    check: PayerCheck,
    command_id: UUID,
) -> PayerRequest:
    """Create an accountable finance task and an explicit synthetic result."""
    kind, due_at, fixture_outcome = check.kind, check.due_at, check.fixture_outcome
    appointment_id = check.appointment_id
    require_rehearsal()
    if kind not in {"eligibility", "authorization"} or fixture_outcome not in {
        "affirmative",
        "denied",
        "unknown",
    }:
        raise InsuranceInputError
    digest = fingerprint(
        [
            str(membership_id),
            str(contract_id),
            str(appointment_id),
            kind,
            due_at.isoformat(),
            fixture_outcome,
        ]
    )
    with transaction.atomic():
        authority(clinic_id=clinic_id, permission="tiss.manage")
        try:
            appointment = Appointment.objects.get(
                pk=appointment_id, clinic_id=clinic_id
            )
        except Appointment.DoesNotExist as error:
            raise InsuranceDeniedError from error
        target = transition_write_target(appointment)
        acquire_appointment_write_gates(target=target)
        with clinic_write(clinic_id=clinic_id) as clinic:
            try:
                appointment = reload_transition_appointment(
                    target, appointment_id, for_update=True
                )
            except AppointmentAccessDeniedError as error:
                raise InsuranceDeniedError from error
            membership = scoped(
                model=InsuranceMembership, clinic_id=clinic_id, identifier=membership_id
            )
            contract = scoped(
                model=PayerContract, clinic_id=clinic_id, identifier=contract_id
            )
            supported = (
                contract.eligibility_supported
                if kind == "eligibility"
                else contract.authorization_supported
            )
            if (
                membership.payer_id != contract.payer_id
                or not supported
                or appointment.payer_membership_id != membership_id
                or appointment.payer_contract_id != contract_id
            ):
                raise InsuranceDeniedError
            existing = replay(
                model=PayerRequest,
                clinic_id=clinic_id,
                command_id=command_id,
                digest=digest,
            )
            if existing is not None:
                return existing
            task = create_task(
                clinic_id=clinic_id,
                spec=TaskSpec(
                    kind="checklist",
                    subject_ref={
                        "kind": "enrollment",
                        "id": str(membership.enrollment_id),
                    },
                    due_at=due_at,
                ),
                idempotency_key=uuid5(command_id, "payer-check"),
            )
            task = assign_task(
                clinic_id=clinic_id,
                task_id=task.pk,
                owner=TaskOwner(
                    user_id=authority(clinic_id=clinic_id, permission="tiss.manage")
                ),
                expected_revision=task.revision,
            )
            outcome = fixture_outcome
            if outcome == "affirmative":
                outcome = "eligible" if kind == "eligibility" else "authorized"
            row = PayerRequest.objects.create(
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
                command_id=command_id,
                fingerprint=digest,
                membership=membership,
                contract=contract,
                appointment=appointment,
                kind=kind,
                outcome=outcome,
                task=task,
                authorization_reference=(
                    "SYN-" + command_id.hex if outcome == "authorized" else ""
                ),
                evidence={"adapter": "synthetic-tiss-v1", "fixture": fixture_outcome},
            )
            audit(clinic_id=clinic_id, row=row, event="insurance.request.recorded")
            return row
