from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.ehr.services import open_encounter
from apps.insurance import services
from apps.insurance.models import Batch, Glosa, Guide
from apps.intake.models import InsuranceMembership as RegisteredMembership
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.models import Appointment
from apps.scheduling.services import complete, start

from billing.journal_support import FinanceSetup, finance_scope, invoice_terms
from ehr.test_autosave import registration as register_physician
from identity.permission_support import owner_context, permission_context
from insurance.fixture_xml import return_xml

if TYPE_CHECKING:
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class Cycle:
    finance: FinanceSetup
    guide: Guide
    batch: Batch
    glosa: Glosa
    original_return: bytes
    appeal_id: UUID


def build_cycle(
    setup: FinanceSetup,
    *,
    membership_number: str = "SYNTHETIC-001",
    professional_name: str = "Synthetic Physician",
) -> Cycle:
    terms = invoice_terms(setup, amount_minor=12345, payer="insurer", completed=False)
    assert terms.invoice.appointment_id is not None
    appointment_id = terms.invoice.appointment_id
    register_physician(setup.graph, setup.graph.physician, setup.clinic_id)
    with permission_context(setup.graph, setup.graph.physician):
        encounter = open_encounter(
            clinic_id=setup.clinic_id, appointment_id=appointment_id
        )
        appointment = Appointment.objects.get(pk=appointment_id)
        running = start(
            clinic_id=setup.clinic_id,
            appointment_id=appointment_id,
            expected_revision=appointment.revision,
            command_id=uuid4(),
        )
        complete(
            clinic_id=setup.clinic_id,
            appointment_id=appointment_id,
            expected_revision=running.revision,
            command_id=uuid4(),
        )
    with owner_context(setup.graph.organization_a):
        enrollment = PatientClinicEnrollment.objects.get(
            patient_id=setup.appointment.patient_id, clinic_id=setup.clinic_id
        )
        appointment = Appointment.objects.get(pk=appointment_id)
        registration = RegisteredMembership.objects.create(
            organization_id=setup.graph.organization_a,
            patient_id=appointment.patient_id,
            payer_name="Synthetic Payer",
            ans_number="000001",
            membership_number=membership_number,
        )
        service = (
            str(appointment.service_type_id)
            if appointment.service_type_id
            else "unscheduled"
        )
    with finance_scope(setup):
        payer = services.create_payer(
            clinic_id=setup.clinic_id,
            ans_registry="000001",
            name="Synthetic Payer",
            command_id=uuid4(),
        )
        services.backfill_membership_payers(clinic_id=setup.clinic_id)
        contract = services.create_contract(
            clinic_id=setup.clinic_id,
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
                        str(setup.graph.physician): {
                            "name": professional_name,
                            "council": "06",
                            "number": "123456",
                            "uf": "35",
                            "cbo": "225125",
                        }
                    },
                },
                channels=frozenset({"webservice", "eligibility", "authorization"}),
            ),
            command_id=uuid4(),
        )
        services.import_tuss(
            clinic_id=setup.clinic_id,
            component_version="202601",
            source_date=date(2026, 1, 1),
            source=b"table,code,description\n22,10101012,Synthetic consultation\n",
            command_id=uuid4(),
        )
        prices = services.create_price_table(
            clinic_id=setup.clinic_id,
            contract_id=contract.pk,
            version=1,
            prices={
                service: {
                    "table": "22",
                    "code": "10101012",
                    "quantity": 1,
                    "unit_minor": 12345,
                }
            },
            command_id=uuid4(),
        )
        membership = services.register_membership(
            clinic_id=setup.clinic_id,
            payer_id=payer.pk,
            enrollment_id=enrollment.pk,
            terms=services.MembershipTerms(registration.pk, date(2020, 1, 1), "N"),
            command_id=uuid4(),
        )
        guide = services.draft_guide(
            clinic_id=setup.clinic_id,
            selection=services.GuideSelection(
                encounter.pk, terms.invoice_id, membership.pk, prices.pk, "spsadt"
            ),
            command_id=uuid4(),
        )
        services.validate_guide(
            clinic_id=setup.clinic_id, guide_id=guide.pk, command_id=uuid4()
        )
        batch = services.create_batch(
            clinic_id=setup.clinic_id,
            contract_id=contract.pk,
            guide_ids=(guide.pk,),
            command_id=uuid4(),
        )
        services.validate_batch(
            clinic_id=setup.clinic_id, batch_id=batch.pk, command_id=uuid4()
        )
        services.seal_batch(
            clinic_id=setup.clinic_id, batch_id=batch.pk, command_id=uuid4()
        )
        services.prepare_submission(
            clinic_id=setup.clinic_id,
            batch_id=batch.pk,
            channel="portal",
            command_id=uuid4(),
        )
        for outcome in ("submitted", "acknowledged"):
            services.submission_outcome(
                clinic_id=setup.clinic_id,
                batch_id=batch.pk,
                outcome=outcome,
                command_id=uuid4(),
            )
        xml = return_xml(guide=guide.number, card=membership_number)
        source = services.ingest_return(
            clinic_id=setup.clinic_id, batch_id=batch.pk, xml=xml, command_id=uuid4()
        )
        glosa = Glosa.objects.get(clinic_id=setup.clinic_id, source=source)
        appeal = services.draft_appeal(
            clinic_id=setup.clinic_id,
            glosa_id=glosa.pk,
            body="Synthetic administrative appeal.",
            clinical_assertions="Synthetic assertion for physician review.",
            command_id=uuid4(),
        )
    return Cycle(setup, guide, batch, glosa, xml, appeal.pk)
