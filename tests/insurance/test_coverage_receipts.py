from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.audit.models import AuditEvent
from apps.comms.models import IntegrationOperation
from apps.identity.models import UserClinicRole
from apps.insurance import appointment_coverage, services
from apps.insurance.models import CommandReceipt
from apps.intake.demographics import save_insurance_membership
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.models import Appointment
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.services import book
from apps.tenancy import envelope
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from billing.journal_support import finance_scope
from identity.permission_support import owner_context
from insurance.test_registered_booking import Insured, hold, insured
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.intake.models import InsuranceMembership as Registration

pytestmark = pytest.mark.django_db(transaction=True)
__all__ = ("insured",)


def _authorized_hold(selected: Insured) -> Appointment:
    appointment = hold(selected)
    with finance_scope(selected.setup):
        for kind in ("eligibility", "authorization"):
            services.request_payer_check(
                clinic_id=selected.setup.clinic_id,
                membership_id=selected.membership.pk,
                contract_id=selected.contract.pk,
                check=services.PayerCheck(
                    appointment.pk,
                    kind,
                    datetime(2035, 6, 1, tzinfo=UTC),
                    "affirmative",
                ),
                command_id=uuid4(),
            )
        appointment.refresh_from_db()
        assert (
            CommandReceipt.objects.filter(
                kind="coverage", subject_id=appointment.pk
            ).count()
            == 1
        )
    return appointment


def _expired_successor(selected: Insured) -> Registration:
    setup = selected.setup
    with (
        runtime_role(),
        tenant_context(setup.appointment.actor_id, setup.graph.organization_a),
    ):
        return save_insurance_membership(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.appointment.enrollment_id,
            sequence=selected.registration.sequence,
            expected_version=selected.registration.version,
            membership={
                "payer_name": "Synthetic Payer",
                "ans_number": "000001",
                "membership_number": "SYNTHETIC-REGISTERED",
                "plan_name": "Synthetic Plan",
                "valid_until": date(2030, 1, 1),
            },
        )


def _candidate(source: Appointment) -> Appointment:
    return Appointment(
        organization_id=source.organization_id,
        clinic_id=source.clinic_id,
        patient_id=source.patient_id,
        practitioner_id=source.practitioner_id,
        start_at=source.start_at + timedelta(hours=1),
        end_at=source.end_at + timedelta(hours=1),
        service_type_id=source.service_type_id,
        resource_ids=source.resource_ids,
        payer_membership_id=source.payer_membership_id,
        payer_contract_id=source.payer_contract_id,
        status="held",
        idempotency_key=uuid4(),
        create_fingerprint=b"r" * 32,
    )


@pytest.mark.parametrize("changed", ["membership", "contract"])
def test_issued_receipt_cannot_book_after_coverage_changes(
    insured: Insured, changed: str
) -> None:
    appointment = _authorized_hold(insured)
    setup = insured.setup
    if changed == "membership":
        successor = _expired_successor(insured)
        assert successor.version > insured.registration.version
    else:
        with finance_scope(setup):
            services.create_contract(
                clinic_id=setup.clinic_id,
                payer_id=insured.payer.pk,
                terms=services.ContractTerms(
                    insured.contract.version + 1,
                    date(2020, 1, 1),
                    date(2030, 1, 1),
                    insured.contract.billing_identity,
                    frozenset({"eligibility", "authorization"}),
                ),
                command_id=uuid4(),
            )
    with (
        runtime_role(),
        tenant_context(setup.appointment.actor_id, setup.graph.organization_a),
    ):
        with pytest.raises(SchedulingRuleError) as denied:
            book(
                clinic_id=setup.clinic_id,
                appointment_id=appointment.pk,
                expected_revision=appointment.revision,
                command_id=uuid4(),
            )
        assert denied.value.code == "payer_refused"
        appointment.refresh_from_db()
        assert appointment.status == "held"


def test_runtime_cannot_forge_a_matching_coverage_receipt(insured: Insured) -> None:
    appointment = _authorized_hold(insured)
    setup = insured.setup
    with owner_context(setup.graph.organization_a):
        UserClinicRole.objects.get_or_create(
            organization_id=setup.graph.organization_a,
            clinic_id=setup.clinic_id,
            user_id=setup.graph.clinic_admin,
            role="receptionist",
        )
    with finance_scope(setup), connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.has_permission('appointment.book',%s,NULL),"
            "clinic_app.has_permission('tiss.manage',%s,NULL)",
            [setup.clinic_id, setup.clinic_id],
        )
        assert cursor.fetchone() == (True, True)
        proof = CommandReceipt.objects.get(kind="coverage", subject_id=appointment.pk)
        with pytest.raises(DatabaseError) as denied, transaction.atomic():
            CommandReceipt.objects.create(
                organization_id=setup.graph.organization_a,
                clinic_id=setup.clinic_id,
                command_id=uuid4(),
                fingerprint=proof.fingerprint,
                kind="coverage",
                subject_id=appointment.pk,
                next_state="covered",
            )
        cause = denied.value.__cause__
        assert isinstance(cause, psycopg.Error)
        assert cause.sqlstate == "42501"
        assert "row-level security" in str(cause)


def test_database_rejects_an_insured_booking_without_a_receipt(
    insured: Insured,
) -> None:
    candidate = _candidate(hold(insured))
    setup = insured.setup
    with (
        runtime_role(),
        tenant_context(setup.appointment.actor_id, setup.graph.organization_a),
        pytest.raises(DatabaseError) as denied,
        transaction.atomic(),
    ):
        candidate.save(force_insert=True)
    cause = denied.value.__cause__
    assert isinstance(cause, psycopg.Error)
    assert cause.diag.constraint_name == "scheduling_payer_refused"


def _metadata(selected: Insured) -> tuple[str, tuple[int, int, int]]:
    with finance_scope(selected.setup):
        receipts = list(
            CommandReceipt.objects.filter(clinic_id=selected.setup.clinic_id)
            .order_by("pk")
            .values()
        )
    with owner_context(selected.setup.graph.organization_a):
        rows = (
            receipts,
            list(AuditEvent.objects.order_by("pk").values()),
            list(IntegrationOperation.objects.order_by("pk").values()),
        )
    return json.dumps(rows, default=str, sort_keys=True), (
        len(rows[0]),
        len(rows[1]),
        len(rows[2]),
    )


@pytest.mark.parametrize("failure", ["expired", "wrong_key"])
def test_failed_validation_keeps_sensitive_values_out_of_owned_outputs(
    insured: Insured,
    failure: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    candidate = _candidate(_authorized_hold(insured))
    setup = insured.setup
    key = envelope._kek()
    wrong_key = ("0" if key[0] != "0" else "1") + key[1:]
    if failure == "expired":
        successor = _expired_successor(insured)
        with finance_scope(setup):
            services.backfill_membership_payers(clinic_id=setup.clinic_id)
            member = services.register_membership(
                clinic_id=setup.clinic_id,
                payer_id=insured.payer.pk,
                enrollment_id=setup.appointment.enrollment_id,
                terms=services.MembershipTerms(successor.pk, date(2020, 1, 1), "N"),
                command_id=uuid4(),
            )
            candidate.payer_membership_id = member.pk
    else:
        monkeypatch.setattr(appointment_coverage, "_kek", lambda: wrong_key)
    before, counts = _metadata(insured)
    assert counts[0] > 0
    assert counts[1] > 0
    caplog.clear()
    expected = (
        SchedulingRuleError if failure == "expired" else AppointmentAccessDeniedError
    )
    with (
        runtime_role(),
        tenant_context(setup.appointment.actor_id, setup.graph.organization_a),
        pytest.raises(expected) as denied,
        transaction.atomic(),
    ):
        appointment_coverage.record_booking_coverage(
            clinic_id=setup.clinic_id, appointment=candidate
        )
    messages = []
    error: BaseException | None = denied.value
    while error is not None:
        messages.append(str(error))
        error = error.__cause__
    after, after_counts = _metadata(insured)
    changed = before != after
    assert not changed
    assert counts == after_counts
    evidence = {
        "channels": "exception,application_log,audit,receipt,outbox",
        "leaks": 0,
    }
    channels = ("\n".join(messages), caplog.text, before, after, json.dumps(evidence))
    sensitive = (key, wrong_key, "2020-01-01", "2030-01-01", "2036-06-02")
    leaked = any(value in channel for value in sensitive for channel in channels)
    assert not leaked
    for name, value in evidence.items():
        record_property("coverage_privacy_" + name, value)


def test_receipt_reuses_the_existing_envelope_helpers() -> None:
    bindings = appointment_coverage.record_booking_coverage.__globals__
    assert bindings["_call_scalar"] is envelope._call_scalar
    assert bindings["_kek"] is envelope._kek
