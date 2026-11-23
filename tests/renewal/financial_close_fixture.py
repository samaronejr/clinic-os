"""Service-only fiscal and insurer legs of the reference clinic's J6 close."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID, uuid4

from apps.billing.collections import (
    observe_settlement,
    post_settlement,
    verify_settlement,
)
from apps.billing.compensation import prepare_invoice_terms, record_completion
from apps.billing.fiscal import prepare_fiscal_document, submit_fiscal_rehearsal
from apps.billing.journal import create_book
from apps.billing.models import FiscalConfiguration, JournalBook, JournalEntry, Receipt
from apps.billing.money_types import SettlementObservation
from apps.billing.services import create_invoice, issue_invoice
from apps.ehr.services import open_unscheduled_encounter
from apps.identity.models import ProfessionalRegistration
from apps.insurance import services
from apps.insurance.models import Glosa, Guide
from apps.intake.demographics import save_insurance_membership
from apps.tenancy.db import tenant_context
from django.test import override_settings
from django.utils import timezone

from identity.permission_support import owner_context
from insurance.fixture_xml import return_xml
from patient_service_support import runtime_role

CHARGED = 12345
ACCEPTED = 8345
DENIED = 4000
CARD = "SYNTHETIC-J6-001"


def _fiscal(data: dict[str, str]) -> None:
    """Authorize a local rehearsal for the browser's invoice without posting cash."""
    clinic, organization, actor, invoice = (
        UUID(data[key]) for key in ("clinic", "organization", "actor", "invoice")
    )
    with runtime_role(), tenant_context(actor, organization):
        book = JournalBook.objects.get(clinic_id=clinic)
    # Only the externally supplied review configuration is owner-provisioned.
    with owner_context(organization):
        configuration = FiscalConfiguration.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            book=book,
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
    with runtime_role(), tenant_context(actor, organization):
        entries = JournalEntry.objects.filter(book=book).count()
        receipts = Receipt.objects.filter(invoice__clinic_id=clinic).count()
        draft = prepare_fiscal_document(
            clinic_id=clinic,
            invoice_id=invoice,
            configuration_id=configuration.pk,
            command_id=uuid4(),
        )
        command = uuid4()
        authorized = submit_fiscal_rehearsal(
            clinic_id=clinic, document_id=draft.pk, command_id=command
        )
        replay = submit_fiscal_rehearsal(
            clinic_id=clinic, document_id=draft.pk, command_id=command
        )
        assert authorized.pk == replay.pk
        assert authorized.invoice_id == invoice
        assert authorized.status == "authorized"
        assert authorized.rehearsal
        assert b"SYNTHETIC-NOT-FISCAL" in authorized.xml_payload
        assert JournalEntry.objects.filter(book=book).count() == entries
        assert Receipt.objects.filter(invoice__clinic_id=clinic).count() == receipts


def _guide(data: dict[str, str]) -> Guide:
    """Bind a second charge to this patient's attested unscheduled care."""
    clinic, organization, physician, actor, enrollment = (
        UUID(data[key])
        for key in ("clinic", "organization", "physician", "actor", "enrollment")
    )
    with owner_context(organization):
        ProfessionalRegistration.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            user_id=physician,
            role="physician",
            council="CRM",
            number="SINTETICO-J6",
            jurisdiction="SP",
            specialty="Sintetico",
            status="regular",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )
    with runtime_role(), tenant_context(physician, organization):
        encounter = open_unscheduled_encounter(
            clinic_id=clinic, enrollment_id=enrollment, reason="documentation_only"
        )
    with runtime_role(), tenant_context(UUID(data["receptionist"]), organization):
        registered = save_insurance_membership(
            clinic_id=clinic,
            enrollment_id=enrollment,
            sequence=1,
            expected_version=0,
            membership={
                "payer_name": "Synthetic Payer",
                "ans_number": "000001",
                "membership_number": CARD,
            },
        )
    with runtime_role(), tenant_context(actor, organization):
        invoice = create_invoice(
            clinic_id=clinic,
            patient_id=UUID(data["patient"]),
            amount_minor=CHARGED,
            encounter_id=encounter.pk,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=clinic, invoice_id=invoice.pk, expected_revision=invoice.revision
        )
        prepare_invoice_terms(clinic_id=clinic, invoice_id=invoice.pk, payer="insurer")
        record_completion(
            clinic_id=clinic, invoice_id=invoice.pk, evidence_reference=uuid4()
        )
        payer = services.create_payer(
            clinic_id=clinic,
            ans_registry="000001",
            name="Synthetic Payer",
            command_id=uuid4(),
        )
        services.backfill_membership_payers(clinic_id=clinic)
        contract = services.create_contract(
            clinic_id=clinic,
            payer_id=payer.pk,
            terms=services.ContractTerms(
                version=1,
                starts_on=date(2020, 1, 1),
                ends_on=None,
                billing_identity={
                    "code": "SYNTHETIC001",
                    "name": "Synthetic Clinic",
                    "cnes": "0000001",
                    "professionals": {
                        str(physician): {
                            "name": "Synthetic Physician",
                            "council": "06",
                            "number": "123456",
                            "uf": "35",
                            "cbo": "225125",
                        }
                    },
                },
                channels=frozenset({"webservice"}),
            ),
            command_id=uuid4(),
        )
        services.import_tuss(
            clinic_id=clinic,
            component_version="202601",
            source_date=date(2026, 1, 1),
            source=b"table,code,description\n22,10101012,Synthetic consultation\n",
            command_id=uuid4(),
        )
        prices = services.create_price_table(
            clinic_id=clinic,
            contract_id=contract.pk,
            version=1,
            prices={
                "unscheduled": {
                    "table": "22",
                    "code": "10101012",
                    "quantity": 1,
                    "unit_minor": CHARGED,
                }
            },
            command_id=uuid4(),
        )
        membership = services.register_membership(
            clinic_id=clinic,
            payer_id=payer.pk,
            enrollment_id=enrollment,
            terms=services.MembershipTerms(registered.pk, date(2020, 1, 1), "N"),
            command_id=uuid4(),
        )
        return services.draft_guide(
            clinic_id=clinic,
            selection=services.GuideSelection(
                encounter.pk, invoice.pk, membership.pk, prices.pk, "spsadt"
            ),
            command_id=uuid4(),
        )


def _payer_denial(data: dict[str, str], guide: Guide) -> Glosa:
    """Build a real guide-bound adjudication, independent of cash and appeals."""
    clinic, organization, actor = (
        UUID(data[key]) for key in ("clinic", "organization", "actor")
    )
    with runtime_role(), tenant_context(actor, organization):
        services.validate_guide(clinic_id=clinic, guide_id=guide.pk, command_id=uuid4())
        batch = services.create_batch(
            clinic_id=clinic,
            contract_id=guide.contract_id,
            guide_ids=(guide.pk,),
            command_id=uuid4(),
        )
        services.validate_batch(clinic_id=clinic, batch_id=batch.pk, command_id=uuid4())
        services.seal_batch(clinic_id=clinic, batch_id=batch.pk, command_id=uuid4())
        services.prepare_submission(
            clinic_id=clinic, batch_id=batch.pk, channel="portal", command_id=uuid4()
        )
        for outcome in ("submitted", "acknowledged"):
            services.submission_outcome(
                clinic_id=clinic,
                batch_id=batch.pk,
                outcome=outcome,
                command_id=uuid4(),
            )
        before_return = JournalEntry.objects.filter(book__clinic_id=clinic).count()
        xml = return_xml(guide=guide.number, card=CARD)
        returned = services.ingest_return(
            clinic_id=clinic, batch_id=batch.pk, xml=xml, command_id=uuid4()
        )
        replay = services.ingest_return(
            clinic_id=clinic, batch_id=batch.pk, xml=xml, command_id=uuid4()
        )
        assert replay.pk == returned.pk
        glosa = Glosa.objects.get(clinic_id=clinic, source=returned, guide=guide)
        assert (glosa.charged_minor, glosa.accepted_minor, glosa.denied_minor) == (
            CHARGED,
            ACCEPTED,
            DENIED,
        )
        assert (
            JournalEntry.objects.filter(book__clinic_id=clinic).count() == before_return
        )
        return glosa


def _reconcile(data: dict[str, str], guide: Guide) -> None:
    """Retain adjudication separately from the independently verified payer cash."""
    _payer_denial(data, guide)
    clinic, organization, actor = (
        UUID(data[key]) for key in ("clinic", "organization", "actor")
    )
    with runtime_role(), tenant_context(actor, organization):
        settlement = observe_settlement(
            clinic_id=clinic,
            observation=SettlementObservation(ACCEPTED, "insurer", "bank", uuid4()),
        )
        verify_settlement(clinic_id=clinic, settlement_id=settlement.pk)
        post_settlement(clinic_id=clinic, settlement_id=settlement.pk)
        command = uuid4()
        matched = services.reconcile_payment(
            clinic_id=clinic,
            guide_id=guide.pk,
            settlement_id=settlement.pk,
            amount_minor=ACCEPTED,
            command_id=command,
        )
        replay_match = services.reconcile_payment(
            clinic_id=clinic,
            guide_id=guide.pk,
            settlement_id=settlement.pk,
            amount_minor=ACCEPTED,
            command_id=command,
        )
        assert matched.pk == replay_match.pk
        assert matched.allocation.terms.invoice_id == guide.invoice_id
        assert matched.allocation.amount_minor == ACCEPTED


def manual_denial(data: dict[str, str]) -> dict[str, object]:
    """Seed once per clinic: all four AI-off cases handle the very same denial."""
    clinic, org = UUID(data["clinic"]), UUID(data["organization"])
    with runtime_role(), tenant_context(UUID(data["actor"]), org):
        denial = Glosa.objects.filter(clinic_id=clinic, denied_minor=DENIED).first()
    if denial is None:
        with runtime_role(), tenant_context(UUID(data["administrator"]), org):
            create_book(clinic_id=clinic)
        with override_settings(INSURANCE_SYNTHETIC_ENABLED=True):
            denial = _payer_denial(data, _guide(data))
    return {"guide": str(denial.guide_id), "denial": str(denial.pk)}


def reconcile_documents(data: dict[str, str]) -> dict[str, object]:
    """Run explicit local rehearsals; no live configuration or provider is enabled."""
    with override_settings(
        BILLING_SYNTHETIC_NFSE=True, INSURANCE_SYNTHETIC_ENABLED=True
    ):
        _fiscal(data)
        guide = _guide(data)
        _reconcile(data, guide)
    return {
        "fiscal_authorized": 1,
        "insurer_charged_minor": CHARGED,
        "insurer_collected_minor": ACCEPTED,
        "insurer_outstanding_minor": DENIED,
        "return_replay_idempotent": True,
        "payment_replay_idempotent": True,
    }
