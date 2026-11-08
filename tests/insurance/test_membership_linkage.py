from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, date, datetime
from importlib import import_module
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from apps.identity.models import Clinic
from apps.insurance import appointment_coverage
from apps.insurance.appointment_coverage import BookingCoverage
from apps.insurance.memberships import payer_match
from apps.intake.models import PatientClinicEnrollment
from apps.scheduling import appointment_creation as creation
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.appointment_values import (
    AppointmentLocalRange,
    CreateAppointmentRequest,
    PreparedAppointment,
)
from apps.scheduling.models import Appointment
from apps.scheduling.resource_errors import SchedulingRuleError
from django.db import models, transaction
from django.db.migrations.loader import MigrationLoader
from ops.testing.restore_contract import DOMAIN_RELATIONS

if TYPE_CHECKING:
    from collections.abc import Callable

IDS = tuple(UUID(int=index) for index in range(1, 13))


def coverage() -> BookingCoverage:
    return BookingCoverage(
        IDS[4],
        IDS[5],
        IDS[1],
        IDS[2],
        IDS[6],
        IDS[6],
        date(2020, 1, 1),
        None,
        date(2020, 1, 1),
        None,
        eligibility_supported=False,
        authorization_supported=False,
    )


@pytest.mark.parametrize(
    ("ans", "choices", "expected"),
    [
        ("123456", {"123456": (IDS[0],)}, IDS[0]),
        ("123456", {"123456": (IDS[0], IDS[1])}, None),
        ("123456", {"654321": (IDS[0],)}, None),
        ("", {"123456": (IDS[0],)}, None),
        ("12345", {"012345": (IDS[0],)}, None),
        (" 123456", {"123456": (IDS[0],)}, None),
        ("Synthetic Payer", {"123456": (IDS[0],)}, None),
    ],
)
def test_backfill_never_guesses_a_payer(
    ans: str, choices: dict[str, tuple[UUID, ...]], expected: UUID | None
) -> None:
    assert payer_match(ans, choices) == expected


def test_intake_payer_link_exists_in_the_migration_state() -> None:
    state = MigrationLoader(None).project_state()
    field = state.models["intake", "insurancemembership"].fields.get("payer")
    assert isinstance(field, models.ForeignKey)
    assert field.null
    assert field.deconstruct()[3].get("db_constraint", True)
    assert field.deconstruct()[3]["to"] == "insurance.payer"
    registration = state.models["insurance", "insurancemembership"].fields.get(
        "registration"
    )
    assert isinstance(registration, models.ForeignKey)
    assert registration.deconstruct()[3]["to"] == "intake.insurancemembership"


def test_appointment_membership_is_a_real_foreign_key_in_migration_state() -> None:
    state = MigrationLoader(None).project_state()
    fields = state.models["scheduling", "appointment"].fields
    member = fields.get("payer_membership")
    assert isinstance(member, models.ForeignKey)
    assert member.deconstruct()[3].get("db_constraint", True)
    assert member.null
    assert member.deconstruct()[3]["to"] == "insurance.insurancemembership"
    for name in ("eligibility_check", "authorization_check"):
        field = fields[name]
        assert isinstance(field, models.ForeignKey)
        assert field.deconstruct()[3]["to"] == "insurance.payerrequest"
    request = state.models["insurance", "payerrequest"].fields["appointment"]
    assert isinstance(request, models.ForeignKey)
    assert request.deconstruct()[3]["to"] == "scheduling.appointment"


@pytest.fixture
def book_service(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[BookingCoverage], Appointment]:
    """Keep the real booking/coverage integration; replace unrelated DB writes."""
    clinic = Clinic(pk=IDS[1], organization_id=IDS[0], timezone="America/Sao_Paulo")
    enrollment = PatientClinicEnrollment(
        pk=IDS[3], organization_id=IDS[0], clinic_id=IDS[1], patient_id=IDS[2]
    )
    prepared = PreparedAppointment(
        enrollment,
        datetime(2035, 1, 2, 12, tzinfo=UTC),
        datetime(2035, 1, 2, 12, 30, tzinfo=UTC),
        b"x" * 32,
    )
    monkeypatch.setattr(transaction, "atomic", lambda *a, **k: nullcontext())
    monkeypatch.setattr(creation, "_request_clinic", lambda request: clinic)
    monkeypatch.setattr(creation, "booking_selection", lambda **kwargs: None)
    monkeypatch.setattr(creation, "prepare_appointment", lambda *args: prepared)
    monkeypatch.setattr(creation, "_replay_for_request", lambda *args: None)
    monkeypatch.setattr(creation, "validate_new_appointment", lambda *args: None)
    monkeypatch.setattr(creation, "acquire_appointment_write_gates", lambda **k: None)
    monkeypatch.setattr(creation, "_require_practitioner", lambda *args: None)
    monkeypatch.setattr(creation, "validate_resource_window", lambda **kwargs: None)
    monkeypatch.setattr(creation, "lock_appointment_write_rows", lambda **k: object())
    monkeypatch.setattr(
        creation,
        "_post_lock_revalidate",
        lambda *args: (clinic, prepared, None),
    )
    monkeypatch.setattr(creation, "record_appointment_event", lambda *a, **k: None)

    def insert(
        selected: Clinic,
        request: CreateAppointmentRequest,
        values: PreparedAppointment,
    ) -> tuple[Appointment, bool]:
        return Appointment(
            pk=IDS[7],
            organization_id=selected.organization_id,
            clinic_id=selected.pk,
            patient_id=values.enrollment.patient_id,
            payer_membership_id=request.payer_membership_id,
            payer_contract_id=request.payer_contract_id,
        ), True

    monkeypatch.setattr(creation, "insert_appointment", insert)

    def submit(selected: BookingCoverage) -> Appointment:
        monkeypatch.setattr(
            appointment_coverage, "booking_coverage", lambda **kwargs: selected
        )
        return creation.create_service_appointment(
            clinic_id=clinic.pk,
            enrollment_id=enrollment.pk,
            practitioner_id=IDS[8],
            booking=creation.ServiceBooking(
                AppointmentLocalRange("2035-01-02T09:00", "2035-01-02T09:30"),
                IDS[9],
                payer_membership_id=IDS[4],
                payer_contract_id=IDS[5],
            ),
            idempotency_key=IDS[10],
        )

    return submit


def test_existing_booking_entrypoint_retains_registered_membership(
    book_service: Callable[[BookingCoverage], Appointment],
) -> None:
    result = book_service(coverage())
    assert result.payer_membership_id == IDS[4]
    assert result.payer_contract_id == IDS[5]


@pytest.mark.parametrize(
    "foreign",
    [
        replace(coverage(), clinic_id=IDS[11]),
        replace(coverage(), patient_id=IDS[11]),
        replace(coverage(), contract_payer_id=IDS[11]),
    ],
)
def test_existing_booking_entrypoint_refuses_foreign_membership_or_payer(
    book_service: Callable[[BookingCoverage], Appointment],
    foreign: BookingCoverage,
) -> None:
    with pytest.raises(AppointmentAccessDeniedError):
        book_service(foreign)


def test_existing_booking_entrypoint_refuses_expired_payer_coverage(
    book_service: Callable[[BookingCoverage], Appointment],
) -> None:
    with pytest.raises(SchedulingRuleError) as caught:
        book_service(replace(coverage(), valid_to=date(2034, 12, 31)))
    assert caught.value.code == "payer_refused"


def test_supported_payer_checks_need_a_hold_before_confirmation(
    book_service: Callable[[BookingCoverage], Appointment],
) -> None:
    with pytest.raises(SchedulingRuleError) as caught:
        book_service(replace(coverage(), authorization_supported=True))
    assert caught.value.code == "payer_pending"


def test_integration_migrations_are_non_atomic_and_irreversible() -> None:
    for module in (
        "apps.intake.migrations.0029_membership_payer",
        "apps.scheduling.migrations.0011_insurer_membership",
    ):
        migration = import_module(module).Migration
        assert migration.atomic is False
        assert all(not operation.reversible for operation in migration.operations)


def test_membership_fk_sources_and_targets_are_in_the_recovery_inventory() -> None:
    state = MigrationLoader(None).project_state()
    for app, model, names in (
        ("intake", "insurancemembership", ("payer",)),
        ("insurance", "insurancemembership", ("registration",)),
        ("insurance", "payerrequest", ("appointment",)),
        (
            "scheduling",
            "appointment",
            (
                "payer_membership",
                "payer_contract",
                "eligibility_check",
                "authorization_check",
            ),
        ),
    ):
        current = state.apps.get_model(app, model)
        assert current._meta.db_table in DOMAIN_RELATIONS
        for name in names:
            field = current._meta.get_field(name)
            assert isinstance(field, models.ForeignKey)
            assert field.remote_field.model._meta.db_table in DOMAIN_RELATIONS


def test_insurance_integration_is_after_the_runtime_journal_dependency() -> None:
    loader = MigrationLoader(None)
    billing = ("billing", "0011_operational_journal")
    assert ("scheduling", "0010_patient_service_booking") in {
        parent.key for parent in loader.graph.node_map[billing].parents
    }
    for target in (
        ("intake", "0029_membership_payer"),
        ("scheduling", "0011_insurer_membership"),
    ):
        plan = loader.graph.forwards_plan(target)
        assert plan.index(billing) < plan.index(target)
