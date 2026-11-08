from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.billing.models import JournalEntry
from apps.identity.current_context import require_permission
from apps.identity.models import ProfessionalRegistration, RoleGrant, UserClinicRole
from apps.insurance import services
from apps.insurance.errors import InsuranceConflictError, InsuranceDeniedError
from apps.insurance.models import Appeal, Batch, ClinicalApproval, Glosa, Return
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from billing.journal_support import collected, finance_scope
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)

if TYPE_CHECKING:
    from insurance.cycle_support import Cycle

pytestmark = pytest.mark.django_db(transaction=True)


def test_duplicate_return_is_idempotent_and_cannot_post_cash(cycle: Cycle) -> None:
    with finance_scope(cycle.finance):
        before = JournalEntry.objects.filter(book_id=cycle.finance.book_id).count()
        source = services.ingest_return(
            clinic_id=cycle.finance.clinic_id,
            batch_id=cycle.batch.pk,
            xml=cycle.original_return,
            command_id=uuid4(),
        )
        assert source.pk == cycle.glosa.source_id
        assert Return.objects.filter(batch_id=cycle.batch.pk).count() == 1
        assert Glosa.objects.filter(source=source).count() == 1
        assert (
            JournalEntry.objects.filter(book_id=cycle.finance.book_id).count() == before
        )
        assert cycle.glosa.decision == "partially_accepted"


def test_non_clinician_cannot_approve_new_appeal_assertions(cycle: Cycle) -> None:
    with finance_scope(cycle.finance):
        with pytest.raises(InsuranceDeniedError):
            services.approve_appeal_assertions(
                clinic_id=cycle.finance.clinic_id,
                appeal_id=cycle.appeal_id,
                command_id=uuid4(),
            )
        with pytest.raises(InsuranceConflictError):
            services.approve_appeal(
                clinic_id=cycle.finance.clinic_id,
                appeal_id=cycle.appeal_id,
                command_id=uuid4(),
            )
        assert not ClinicalApproval.objects.filter(appeal_id=cycle.appeal_id).exists()


def test_administrative_appeal_needs_no_fabricated_clinical_approval(
    cycle: Cycle,
) -> None:
    with finance_scope(cycle.finance):
        appeal = services.draft_appeal(
            clinic_id=cycle.finance.clinic_id,
            glosa_id=cycle.glosa.pk,
            body="Synthetic administrative correction.",
            clinical_assertions="",
            command_id=uuid4(),
        )
        approved = services.approve_appeal(
            clinic_id=cycle.finance.clinic_id,
            appeal_id=appeal.pk,
            command_id=uuid4(),
        )
        assert approved.state == "approved"
        assert not ClinicalApproval.objects.filter(appeal=appeal).exists()


def test_revoked_clinician_authority_invalidates_existing_approval(
    cycle: Cycle,
) -> None:
    graph = cycle.finance.graph
    with permission_context(graph, graph.physician):
        approval = services.approve_appeal_assertions(
            clinic_id=graph.clinic_a,
            appeal_id=cycle.appeal_id,
            command_id=uuid4(),
        )
        assert approval.approved_by_id == graph.physician
    with owner_context(graph.organization_a):
        revoked = UserClinicRole.objects.filter(
            user_id=graph.physician,
            clinic_id=graph.clinic_a,
            role="physician",
            revoked_at__isnull=True,
        ).update(revoked_at=timezone.now())
        assert revoked == 1
    with finance_scope(cycle.finance):
        with pytest.raises(InsuranceConflictError):
            services.approve_appeal(
                clinic_id=graph.clinic_a,
                appeal_id=cycle.appeal_id,
                command_id=uuid4(),
            )
        assert Appeal.objects.get(pk=cycle.appeal_id).state == "draft"


def test_clinician_from_another_clinic_gets_identical_denial(cycle: Cycle) -> None:
    graph = cycle.finance.graph
    foreign_doctor, _enrollment = permission_actor(graph, "physician")
    now = timezone.now()
    with owner_context(graph.organization_a):
        UserClinicRole.objects.filter(
            user_id=foreign_doctor, clinic_id=graph.clinic_a, role="physician"
        ).update(revoked_at=now)
        UserClinicRole.objects.create(
            user_id=foreign_doctor,
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            role="physician",
        )
        ProfessionalRegistration.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            user_id=foreign_doctor,
            role="physician",
            council="CRM",
            number="SINTETICO-002",
            jurisdiction="RJ",
            specialty="Synthetic",
            status="regular",
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=1),
        )
    with permission_context(graph, foreign_doctor):
        assert (
            require_permission("tiss.clinical_approve", clinic_id=graph.clinic_b)
            == foreign_doctor
        )
        errors = []
        for appeal_id in (cycle.appeal_id, uuid4()):
            with pytest.raises(InsuranceDeniedError) as caught:
                services.approve_appeal_assertions(
                    clinic_id=graph.clinic_a, appeal_id=appeal_id, command_id=uuid4()
                )
            errors.append(str(caught.value))
        assert errors[0] == errors[1]


def test_v2_subtraction_invalidates_retained_clinical_approval(cycle: Cycle) -> None:
    graph = cycle.finance.graph
    with permission_context(graph, graph.physician):
        services.approve_appeal_assertions(
            clinic_id=graph.clinic_a,
            appeal_id=cycle.appeal_id,
            command_id=uuid4(),
        )
    with owner_context(graph.organization_a):
        RoleGrant.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            role="physician",
            permission="tiss.clinical_approve",
            bundle_version=2,
            valid_from=timezone.now(),
        )
    with finance_scope(cycle.finance), pytest.raises(InsuranceConflictError):
        services.approve_appeal(
            clinic_id=graph.clinic_a, appeal_id=cycle.appeal_id, command_id=uuid4()
        )


def test_clinician_approval_appends_only_allowed_audit_metadata(cycle: Cycle) -> None:
    graph = cycle.finance.graph
    command = uuid4()
    with permission_context(graph, graph.physician):
        approval = services.approve_appeal_assertions(
            clinic_id=graph.clinic_a, appeal_id=cycle.appeal_id, command_id=command
        )
        repeated = services.approve_appeal_assertions(
            clinic_id=graph.clinic_a, appeal_id=cycle.appeal_id, command_id=command
        )
        assert repeated.pk == approval.pk
    with owner_context(graph.organization_a):
        events = AuditEvent.objects.filter(
            organization_id=graph.organization_a,
            event_type="insurance.appeal.clinical_approved",
            affected_record_id=str(approval.pk),
        )
        assert events.count() == 1
        event = events.get()
        assert event.actor_user_id == graph.physician
        assert set(event.payload) <= {
            "clinic_id",
            "http_method",
            "http_status",
            "object_verb",
            "reason_code",
            "request_id",
        }
    with finance_scope(cycle.finance):
        services.approve_appeal(
            clinic_id=graph.clinic_a, appeal_id=cycle.appeal_id, command_id=uuid4()
        )
        row = services.submit_appeal(
            clinic_id=graph.clinic_a, appeal_id=cycle.appeal_id, command_id=uuid4()
        )
        assert row.state == "submitted"


def test_payer_cash_reconciliation_posts_once_through_existing_journal(
    cycle: Cycle,
) -> None:
    setup = cycle.finance
    command = uuid4()
    with finance_scope(setup):
        settlement = collected(setup, 8345, kind="insurer")
        match = services.reconcile_payment(
            clinic_id=setup.clinic_id,
            guide_id=cycle.guide.pk,
            settlement_id=settlement.pk,
            amount_minor=8345,
            command_id=command,
        )
        replay = services.reconcile_payment(
            clinic_id=setup.clinic_id,
            guide_id=cycle.guide.pk,
            settlement_id=settlement.pk,
            amount_minor=8345,
            command_id=command,
        )
        assert match.pk == replay.pk
        entry = match.allocation.entry
        assert entry is not None
        assert entry.source_kind == "allocation"
        with pytest.raises(InsuranceConflictError):
            services.reconcile_payment(
                clinic_id=setup.clinic_id,
                guide_id=cycle.guide.pk,
                settlement_id=settlement.pk,
                amount_minor=8345,
                command_id=uuid4(),
            )


def test_rebilling_requires_a_retained_successor_not_a_second_claim_root(
    cycle: Cycle,
) -> None:
    old = cycle.guide
    with finance_scope(cycle.finance):
        with pytest.raises(InsuranceConflictError):
            services.draft_guide(
                clinic_id=cycle.finance.clinic_id,
                selection=services.GuideSelection(
                    old.encounter_id,
                    old.invoice_id,
                    old.membership_id,
                    old.price_table_id,
                    old.kind,
                ),
                command_id=uuid4(),
            )
        successor = services.resubmit_guide(
            clinic_id=cycle.finance.clinic_id,
            guide_id=old.pk,
            command_id=uuid4(),
        )
        assert successor.predecessor_id == old.pk
        assert successor.invoice_id == old.invoice_id
        assert successor.number != old.number


def test_sealed_bytes_and_history_cannot_be_mutated_or_deleted(cycle: Cycle) -> None:
    with finance_scope(cycle.finance):
        for statement in (
            "UPDATE clinic_app.insurance_batch SET xml=decode('00','hex') WHERE id=%s",
            "DELETE FROM clinic_app.insurance_batch WHERE id=%s",
        ):
            with (
                pytest.raises(DatabaseError),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement, [cycle.batch.pk])
        assert (
            Batch.objects.get(pk=cycle.batch.pk).content_sha256
            == cycle.batch.content_sha256
        )
