from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.insurance import services
from apps.insurance.appointment_coverage import record_booking_coverage
from apps.insurance.errors import InsuranceDeniedError
from apps.intake.models import InsuranceMembership as Registration
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.models import Appointment
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.services import (
    AppointmentLocalRange,
    ServiceBooking,
    book,
    create_service_hold,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from billing.journal_support import finance_scope
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from scheduling.test_resources import _catalog

if TYPE_CHECKING:
    from apps.insurance.models import InsuranceMembership, Payer, PayerContract
    from pytest_django.fixtures import SettingsWrapper

    from billing.journal_support import FinanceSetup

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class Insured:
    setup: FinanceSetup
    registration: Registration
    payer: Payer
    contract: PayerContract
    membership: InsuranceMembership
    booking: ServiceBooking
    encrypted_before: tuple[bytes | None, ...]


def ciphertexts(identifier: UUID) -> tuple[bytes | None, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT payer_name,ans_number,membership_number,plan_name,valid_until "
            "FROM clinic_app.intake_insurancemembership WHERE id=%s",
            [identifier],
        )
        row = cursor.fetchone()
        assert row is not None
        return tuple(bytes(value) if value is not None else None for value in row)


@pytest.fixture
def insured(
    finance_setup: FinanceSetup,
    settings: SettingsWrapper,
) -> Insured:
    settings.INSURANCE_SYNTHETIC_ENABLED = True
    setup = finance_setup
    with owner_context(setup.graph.organization_a):
        registration = Registration.objects.create(
            organization_id=setup.graph.organization_a,
            patient_id=setup.appointment.patient_id,
            payer_name="Synthetic Payer",
            ans_number="000001",
            membership_number="SYNTHETIC-REGISTERED",
            plan_name="Synthetic Plan",
            valid_until=date(2036, 6, 2),
        )
        before = ciphertexts(registration.pk)
        UserClinicRole.objects.filter(
            clinic_id=setup.clinic_id,
            user_id=setup.graph.clinic_admin,
            role="finance",
        ).update(workflow_bundle_version=2)
    with (
        runtime_role(),
        tenant_context(setup.appointment.actor_id, setup.graph.organization_a),
    ):
        room, equipment, service = _catalog(setup.appointment)
    with finance_scope(setup):
        payer = services.create_payer(
            clinic_id=setup.clinic_id,
            ans_registry="000001",
            name="Synthetic Payer",
            command_id=uuid4(),
        )
        contract = services.create_contract(
            clinic_id=setup.clinic_id,
            payer_id=payer.pk,
            terms=services.ContractTerms(
                1,
                date(2020, 1, 1),
                None,
                {
                    "code": "SYNTHETIC001",
                    "name": "Synthetic Clinic",
                    "cnes": "0000001",
                    "professionals": {},
                },
                frozenset({"eligibility", "authorization"}),
            ),
            command_id=uuid4(),
        )
        linked = services.backfill_membership_payers(clinic_id=setup.clinic_id)
        assert linked.linked == (registration.pk,)
        membership = services.register_membership(
            clinic_id=setup.clinic_id,
            payer_id=payer.pk,
            enrollment_id=setup.appointment.enrollment_id,
            terms=services.MembershipTerms(registration.pk, date(2020, 1, 1), "N"),
            command_id=uuid4(),
        )
    return Insured(
        setup,
        registration,
        payer,
        contract,
        membership,
        ServiceBooking(
            AppointmentLocalRange("2035-06-02T09:00", "2035-06-02T09:30"),
            service.pk,
            (room.pk, equipment.pk),
            membership.pk,
            contract.pk,
        ),
        before,
    )


def hold(insured: Insured) -> Appointment:
    setup = insured.setup
    with (
        runtime_role(),
        tenant_context(setup.appointment.actor_id, setup.graph.organization_a),
    ):
        return create_service_hold(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.appointment.enrollment_id,
            practitioner_id=setup.appointment.practitioner_id,
            booking=insured.booking,
            idempotency_key=uuid4(),
        )


def test_existing_registration_is_linked_once_without_rewriting_ciphertext(
    insured: Insured,
) -> None:
    setup = insured.setup
    with finance_scope(setup):
        repeated = services.backfill_membership_payers(clinic_id=setup.clinic_id)
        assert repeated.linked == ()
        assert insured.registration.pk in repeated.already_linked
        insured.registration.refresh_from_db()
        assert insured.registration.payer_id == insured.payer.pk
        assert insured.membership.registration_id == insured.registration.pk
        assert insured.membership.details["card"] == "SYNTHETIC-REGISTERED"
        assert ciphertexts(insured.registration.pk) == insured.encrypted_before


@pytest.mark.parametrize("ans", [None, "999999", " 000001"])
def test_unmatched_memberships_stay_null_and_are_reported(
    insured: Insured,
    ans: str | None,
) -> None:
    with owner_context(insured.setup.graph.organization_a):
        unmatched = Registration.objects.create(
            organization_id=insured.setup.graph.organization_a,
            patient_id=insured.setup.appointment.patient_id,
            sequence=2,
            payer_name="Synthetic Payer",
            ans_number=ans,
            membership_number="SYNTHETIC-UNMATCHED",
        )
    with finance_scope(insured.setup):
        report = services.backfill_membership_payers(clinic_id=insured.setup.clinic_id)
        unmatched.refresh_from_db()
        assert unmatched.payer_id is None
        assert unmatched.pk in report.unmatched


def test_visible_foreign_payer_and_membership_cannot_be_used_in_booking(
    insured: Insured,
) -> None:
    setup = insured.setup
    with owner_context(setup.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=setup.graph.organization_a,
            clinic_id=setup.graph.clinic_b,
            user_id=setup.graph.clinic_admin,
            role="finance",
        )
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=setup.graph.organization_a,
            clinic_id=setup.graph.clinic_b,
            patient_id=setup.appointment.patient_id,
            idempotency_key=uuid4(),
            create_fingerprint=b"e" * 32,
        )
        foreign_registration = Registration.objects.create(
            organization_id=setup.graph.organization_a,
            patient_id=setup.appointment.patient_id,
            sequence=2,
            ans_number="000002",
            membership_number="SYNTHETIC-FOREIGN",
        )
    with finance_scope(setup):
        payer = services.create_payer(
            clinic_id=setup.graph.clinic_b,
            ans_registry="000002",
            name="Synthetic Foreign Payer",
            command_id=uuid4(),
        )
        services.backfill_membership_payers(clinic_id=setup.graph.clinic_b)
        member = services.register_membership(
            clinic_id=setup.graph.clinic_b,
            payer_id=payer.pk,
            enrollment_id=enrollment.pk,
            terms=services.MembershipTerms(
                foreign_registration.pk,
                date(2020, 1, 1),
                "N",
            ),
            command_id=uuid4(),
        )
        foreign_contract = services.create_contract(
            clinic_id=setup.graph.clinic_b,
            payer_id=payer.pk,
            terms=services.ContractTerms(
                1,
                date(2020, 1, 1),
                None,
                insured.contract.billing_identity,
                frozenset(),
            ),
            command_id=uuid4(),
        )
        with pytest.raises(InsuranceDeniedError):
            services.register_membership(
                clinic_id=setup.clinic_id,
                payer_id=payer.pk,
                enrollment_id=setup.appointment.enrollment_id,
                terms=services.MembershipTerms(
                    foreign_registration.pk,
                    date(2020, 1, 1),
                    "N",
                ),
                command_id=uuid4(),
            )
    with (
        runtime_role(),
        tenant_context(setup.appointment.actor_id, setup.graph.organization_a),
        pytest.raises(AppointmentAccessDeniedError),
    ):
        create_service_hold(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.appointment.enrollment_id,
            practitioner_id=setup.appointment.practitioner_id,
            booking=ServiceBooking(
                insured.booking.local_range,
                insured.booking.service_type_id,
                insured.booking.resource_ids,
                member.pk,
                insured.contract.pk,
            ),
            idempotency_key=uuid4(),
        )
    denials = []
    with (
        runtime_role(),
        tenant_context(setup.appointment.actor_id, setup.graph.organization_a),
    ):
        for membership_id, contract_id in (
            (uuid4(), insured.contract.pk),
            (member.pk, insured.contract.pk),
            (insured.membership.pk, uuid4()),
            (insured.membership.pk, foreign_contract.pk),
        ):
            candidate = Appointment(
                patient_id=setup.appointment.patient_id,
                payer_membership_id=membership_id,
                payer_contract_id=contract_id,
                start_at=datetime(2035, 6, 2, 12, tzinfo=UTC),
                end_at=datetime(2035, 6, 2, 12, 30, tzinfo=UTC),
            )
            with (
                pytest.raises(AppointmentAccessDeniedError) as denied,
                transaction.atomic(),
            ):
                record_booking_coverage(
                    clinic_id=setup.clinic_id, appointment=candidate
                )
            denials.append((type(denied.value), str(denied.value)))
    assert len(set(denials)) == 1


@pytest.mark.parametrize(
    ("outcome", "code"),
    [("denied", "payer_refused"), ("unknown", "payer_pending")],
)
def test_payer_refusal_and_unknown_cannot_be_confirmed_or_forge_authorization(
    insured: Insured,
    outcome: str,
    code: str,
) -> None:
    appointment = hold(insured)
    setup = insured.setup
    with finance_scope(setup):
        services.request_payer_check(
            clinic_id=setup.clinic_id,
            membership_id=insured.membership.pk,
            contract_id=insured.contract.pk,
            check=services.PayerCheck(
                appointment.pk,
                "eligibility",
                datetime(2035, 6, 1, tzinfo=UTC),
                "affirmative",
            ),
            command_id=uuid4(),
        )
        authorization = services.request_payer_check(
            clinic_id=setup.clinic_id,
            membership_id=insured.membership.pk,
            contract_id=insured.contract.pk,
            check=services.PayerCheck(
                appointment.pk,
                "authorization",
                datetime(2035, 6, 1, tzinfo=UTC),
                outcome,
            ),
            command_id=uuid4(),
        )
        appointment.refresh_from_db()
        assert appointment.authorization_check_id == authorization.pk
        assert appointment.authorization_reference == ""
    with (
        runtime_role(),
        tenant_context(setup.appointment.actor_id, setup.graph.organization_a),
    ):
        with pytest.raises(SchedulingRuleError) as caught:
            book(
                clinic_id=setup.clinic_id,
                appointment_id=appointment.pk,
                expected_revision=appointment.revision,
                command_id=uuid4(),
            )
        assert caught.value.code == code
        appointment.refresh_from_db()
        assert appointment.status == "held"


def test_old_authorization_replay_does_not_replace_a_later_refusal(
    insured: Insured,
) -> None:
    appointment = hold(insured)
    setup = insured.setup
    command = uuid4()
    check = services.PayerCheck(
        appointment.pk,
        "authorization",
        datetime(2035, 6, 1, tzinfo=UTC),
        "affirmative",
    )
    with finance_scope(setup):
        original = services.request_payer_check(
            clinic_id=setup.clinic_id,
            membership_id=insured.membership.pk,
            contract_id=insured.contract.pk,
            check=check,
            command_id=command,
        )
        denied = services.request_payer_check(
            clinic_id=setup.clinic_id,
            membership_id=insured.membership.pk,
            contract_id=insured.contract.pk,
            check=services.PayerCheck(
                appointment.pk,
                "authorization",
                datetime(2035, 6, 1, tzinfo=UTC),
                "denied",
            ),
            command_id=uuid4(),
        )
        replay = services.request_payer_check(
            clinic_id=setup.clinic_id,
            membership_id=insured.membership.pk,
            contract_id=insured.contract.pk,
            check=check,
            command_id=command,
        )
        assert replay.pk == original.pk
        appointment.refresh_from_db()
        assert appointment.authorization_check_id == denied.pk
        assert appointment.authorization_reference == ""


def test_affirmative_checks_bind_the_real_appointment_before_confirmation(
    insured: Insured,
) -> None:
    appointment = hold(insured)
    setup = insured.setup
    with finance_scope(setup):
        requests = [
            services.request_payer_check(
                clinic_id=setup.clinic_id,
                membership_id=insured.membership.pk,
                contract_id=insured.contract.pk,
                check=services.PayerCheck(
                    appointment.pk,
                    kind,
                    datetime(2035, 6, 1, tzinfo=UTC),
                    "affirmative",
                ),
                command_id=uuid4(),
            )
            for kind in ("eligibility", "authorization")
        ]
        appointment.refresh_from_db()
        assert appointment.eligibility_check_id == requests[0].pk
        assert appointment.authorization_check_id == requests[1].pk
        assert (
            appointment.authorization_reference == requests[1].authorization_reference
        )
        assert appointment.authorization_reference.startswith("SYN-")
    with (
        runtime_role(),
        tenant_context(setup.appointment.actor_id, setup.graph.organization_a),
    ):
        confirmed = book(
            clinic_id=setup.clinic_id,
            appointment_id=appointment.pk,
            expected_revision=appointment.revision,
            command_id=uuid4(),
        )
        assert confirmed.status == "scheduled"
        assert confirmed.payer_membership_id == insured.membership.pk
        assert confirmed.payer_contract_id == insured.contract.pk
