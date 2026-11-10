"""Fiscal acceptance under real PostgreSQL roles, guards and encrypted storage."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.fiscal import (
    cancel_fiscal_rehearsal,
    prepare_fiscal_document,
    submit_fiscal_document,
    submit_fiscal_rehearsal,
)
from apps.billing.fiscal_policy import FiscalRefusedError
from apps.billing.fiscal_presentation import fiscal_summaries
from apps.billing.fiscal_provider import (
    FiscalRequest,
    FiscalResponse,
    NationalFiscalProvider,
)
from apps.billing.models import (
    FiscalConfiguration,
    FiscalDocument,
    FiscalTransition,
    JournalEntry,
    Receipt,
)
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from billing.journal_support import (
    FinanceSetup,
    finance_scope,
    finance_setup,
    invoice_terms,
)
from billing.test_fiscal import FixtureTransport
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("finance_setup",)


def configuration(setup: FinanceSetup) -> UUID:
    with owner_context(setup.graph.organization_a):
        row = FiscalConfiguration.objects.create(
            organization_id=setup.graph.organization_a,
            clinic_id=setup.clinic_id,
            book_id=setup.book_id,
            legal_entity_reference=uuid4(),
            legal_entity={"name": "Synthetic clinic", "registration": "SYNTHETIC"},
            provider="national",
            municipality_code="0000000",
            service_code="SYNTHETIC-SERVICE",
            tax_regime="SYNTHETIC",
            ibs_cbs_applicability="not_applicable",
            tax_fields={"ISS": {"classification": "SYNTHETIC"}},
            review_reference=uuid4(),
            review_kind="synthetic_fixture",
            reviewed_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=1),
            cancellation_window_seconds=3600,
        )
    return row.pk


def draft(setup: FinanceSetup) -> FiscalDocument:
    terms = invoice_terms(setup)
    config_id = configuration(setup)
    with finance_scope(setup):
        return prepare_fiscal_document(
            clinic_id=setup.clinic_id,
            invoice_id=terms.invoice_id,
            configuration_id=config_id,
            command_id=uuid4(),
        )


def test_fixture_authorization_is_separate_from_receipts_and_posts_no_money(
    finance_setup: FinanceSetup, settings: SettingsWrapper
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = True
    document = draft(finance_setup)
    command = uuid4()
    with finance_scope(finance_setup):
        receipts = Receipt.objects.count()
        postings = JournalEntry.objects.count()
        result = submit_fiscal_rehearsal(
            clinic_id=finance_setup.clinic_id,
            document_id=document.pk,
            command_id=command,
        )
        retry = submit_fiscal_rehearsal(
            clinic_id=finance_setup.clinic_id,
            document_id=document.pk,
            command_id=command,
        )
        assert result.status == "authorized"
        assert result.rehearsal is True
        assert result.xml_reference == result.pk
        assert b"SYNTHETIC-NOT-FISCAL" in result.xml_payload
        assert result.pk == retry.pk
        assert Receipt.objects.count() == receipts
        assert JournalEntry.objects.count() == postings
        assert FiscalTransition.objects.filter(document=result).count() == 3
        allowed, summaries = fiscal_summaries(clinic_id=finance_setup.clinic_id)
        assert allowed
        assert len(summaries) == 1
        assert summaries[0].reference == document.pk
        assert summaries[0].status == "authorized"


def test_live_issuance_refuses_a_fixture_review(
    finance_setup: FinanceSetup,
) -> None:
    document = draft(finance_setup)
    with finance_scope(finance_setup), pytest.raises(FiscalRefusedError) as refused:
        submit_fiscal_document(
            clinic_id=finance_setup.clinic_id,
            document_id=document.pk,
            command_id=uuid4(),
        )
    assert refused.value.code == "fiscal_configuration_unreviewed"


def test_replay_cannot_change_the_invoice_or_fiscal_review(
    finance_setup: FinanceSetup,
) -> None:
    document = draft(finance_setup)
    with finance_scope(finance_setup):
        with pytest.raises(FiscalRefusedError) as refused:
            prepare_fiscal_document(
                clinic_id=finance_setup.clinic_id,
                invoice_id=uuid4(),
                configuration_id=document.configuration_id,
                command_id=document.last_command_id,
            )
        assert refused.value.code == "fiscal_command_conflict"


def test_wrong_clinic_and_unknown_document_have_identical_denials(
    finance_setup: FinanceSetup,
) -> None:
    document = draft(finance_setup)
    for document_id in (document.pk, uuid4()):
        with (
            runtime_role(),
            tenant_context(
                finance_setup.graph.clinic_admin, finance_setup.graph.organization_a
            ),
            pytest.raises(BillingAccessDeniedError),
        ):
            submit_fiscal_rehearsal(
                clinic_id=finance_setup.graph.clinic_b,
                document_id=document_id,
                command_id=uuid4(),
            )


def test_runtime_cannot_forge_review_or_skip_submission(
    finance_setup: FinanceSetup,
) -> None:
    document = draft(finance_setup)
    with finance_scope(finance_setup):
        for sql, params in (
            (
                "UPDATE billing_fiscalconfiguration SET review_kind='eg9' WHERE id=%s",
                [document.configuration_id],
            ),
            (
                "UPDATE billing_fiscaldocument SET status='authorized',"
                "last_command_id=%s WHERE id=%s",
                [uuid4(), document.pk],
            ),
            ("DELETE FROM billing_fiscaldocument WHERE id=%s", [document.pk]),
            ("TRUNCATE billing_fiscaltransition", []),
        ):
            with (
                pytest.raises(DatabaseError),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(sql, params)


def test_cancellation_records_one_event_across_exact_retries(
    finance_setup: FinanceSetup, settings: SettingsWrapper
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = True
    document = draft(finance_setup)
    with finance_scope(finance_setup):
        result = submit_fiscal_rehearsal(
            clinic_id=finance_setup.clinic_id,
            document_id=document.pk,
            command_id=uuid4(),
        )
        command = uuid4()
        cancelled = cancel_fiscal_rehearsal(
            clinic_id=finance_setup.clinic_id,
            document_id=result.pk,
            command_id=command,
        )
        retry = cancel_fiscal_rehearsal(
            clinic_id=finance_setup.clinic_id,
            document_id=result.pk,
            command_id=command,
        )
        assert cancelled.status == retry.status == "cancelled"
        assert FiscalTransition.objects.filter(document=result).count() == 4


def test_ambiguous_submission_commits_then_recovers_without_resubmitting(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = True
    document = draft(finance_setup)
    command = uuid4()
    submitted: list[str] = []
    original_submit = NationalFiscalProvider.submit

    def ambiguous_submit(
        provider: NationalFiscalProvider, *, request: FiscalRequest
    ) -> None:
        submitted.append(request.dps_id)
        original_submit(provider, request=request)
        raise TimeoutError

    monkeypatch.setattr(NationalFiscalProvider, "submit", ambiguous_submit)
    with finance_scope(finance_setup):
        pending = submit_fiscal_rehearsal(
            clinic_id=finance_setup.clinic_id,
            document_id=document.pk,
            command_id=command,
        )
        assert pending.status == "submitted"
    with finance_scope(finance_setup):
        recovered = submit_fiscal_rehearsal(
            clinic_id=finance_setup.clinic_id,
            document_id=document.pk,
            command_id=command,
        )
        assert recovered.status == "authorized"
        assert submitted == [document.dps_id]
        assert FiscalTransition.objects.filter(document=document).count() == 3


def test_cancellation_after_the_reviewed_deadline_preserves_authorization(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = True
    document = draft(finance_setup)
    with finance_scope(finance_setup):
        authorized = submit_fiscal_rehearsal(
            clinic_id=finance_setup.clinic_id,
            document_id=document.pk,
            command_id=uuid4(),
        )
        assert authorized.cancel_until is not None
        deadline = authorized.cancel_until + timedelta(seconds=1)
        monkeypatch.setattr("apps.billing.fiscal.timezone.now", lambda: deadline)
        with pytest.raises(FiscalRefusedError) as refused:
            cancel_fiscal_rehearsal(
                clinic_id=finance_setup.clinic_id,
                document_id=document.pk,
                command_id=uuid4(),
            )
        assert refused.value.code == "fiscal_cancel_deadline"
        authorized.refresh_from_db()
        assert authorized.status == "authorized"
        assert FiscalTransition.objects.filter(document=document).count() == 3


@pytest.mark.parametrize("foreign_organization", [False, True])
def test_every_fiscal_table_is_hidden_without_exact_clinic_finance(
    finance_setup: FinanceSetup, foreign_organization: bool
) -> None:
    document = draft(finance_setup)
    graph = finance_setup.graph
    with (
        runtime_role(),
        tenant_context(
            graph.shared_user,
            graph.organization_b if foreign_organization else graph.organization_a,
        ),
    ):
        assert not FiscalConfiguration.objects.exists()
        assert not FiscalDocument.objects.exists()
        assert not FiscalTransition.objects.exists()
        assert fiscal_summaries(clinic_id=finance_setup.clinic_id) == (False, ())
    with finance_scope(finance_setup):
        assert FiscalDocument.objects.get(pk=document.pk).configuration_id
        assert FiscalConfiguration.objects.count() == 1
        assert FiscalTransition.objects.count() == 1


def test_recorded_rejection_keeps_only_safe_code_and_metadata_audit(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = True
    document = draft(finance_setup)

    def recorded_provider(
        *, request: FiscalRequest, provider: str
    ) -> NationalFiscalProvider:
        assert provider == "national"
        transport = FixtureTransport("rejected.json")
        envelope = json.loads(transport.responses[0])
        envelope["idDPS"] = request.dps_id
        transport.responses[0] = json.dumps(envelope).encode()
        return NationalFiscalProvider(transport=transport)

    monkeypatch.setattr("apps.billing.fiscal.rehearsal_provider", recorded_provider)
    with finance_scope(finance_setup):
        before = (Receipt.objects.count(), JournalEntry.objects.count())
        rejected = submit_fiscal_rehearsal(
            clinic_id=finance_setup.clinic_id,
            document_id=document.pk,
            command_id=uuid4(),
        )
        assert rejected.status == "rejected"
        assert rejected.rejection_code == "E0004"
        assert rejected.xml_payload is None
        assert before == (Receipt.objects.count(), JournalEntry.objects.count())
    with owner_context(finance_setup.graph.organization_a):
        transitions = list(
            AuditEvent.objects.filter(
                affected_record_id=str(document.pk),
                event_type__startswith="billing.fiscal.",
            ).order_by("seq")
        )
        assert [event.event_type for event in transitions] == [
            "billing.fiscal.draft",
            "billing.fiscal.submitted",
            "billing.fiscal.rejected",
        ]
        for event in transitions:
            assert set(event.payload) <= {
                "clinic_id",
                "http_method",
                "http_status",
                "object_verb",
                "reason_code",
                "request_id",
            }
            assert "SYNTHETIC-PII-SENTINEL" not in json.dumps(event.payload)


def test_unknown_document_selector_leaves_existing_fiscal_history_unchanged(
    finance_setup: FinanceSetup, settings: SettingsWrapper
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = True
    document = draft(finance_setup)
    with finance_scope(finance_setup):
        with pytest.raises(BillingAccessDeniedError):
            submit_fiscal_rehearsal(
                clinic_id=finance_setup.clinic_id,
                document_id=uuid4(),
                command_id=uuid4(),
            )
        assert FiscalTransition.objects.filter(document=document).count() == 1
        document.refresh_from_db()
        assert document.status == "draft"


def test_pending_recorded_response_does_not_authorize_or_create_artifacts(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.BILLING_SYNTHETIC_NFSE = True
    document = draft(finance_setup)

    def pending(
        _provider: NationalFiscalProvider, *, request: FiscalRequest
    ) -> FiscalResponse:
        return FiscalResponse(status="submitted", dps_id=request.dps_id)

    monkeypatch.setattr(NationalFiscalProvider, "submit", pending)
    with finance_scope(finance_setup):
        result = submit_fiscal_rehearsal(
            clinic_id=finance_setup.clinic_id,
            document_id=document.pk,
            command_id=uuid4(),
        )
        assert result.status == "submitted"
        assert result.xml_payload is None
        assert not result.provider_reference
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT provider_reference IS NULL FROM billing_fiscaldocument "
                "WHERE id=%s",
                [result.pk],
            )
            assert cursor.fetchone() == (True,)
        assert FiscalTransition.objects.filter(document=document).count() == 2
