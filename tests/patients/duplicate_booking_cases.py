"""Open-state forward work and independent service/native alias defenses."""

from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Literal, assert_never
from uuid import uuid4

import psycopg
import pytest
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    publish_configuration,
)
from apps.intake import merge_execution
from apps.intake.duplicates import adjudicate_duplicate
from apps.intake.models import PatientMergeTransfer
from apps.intake.patient_access import patient_session_context
from apps.scheduling.models import Appointment, AppointmentTransition
from apps.scheduling.patient_booking import book_patient_slot, patient_slots
from apps.scheduling.services import (
    AppointmentLocalRange,
    arrive,
    book,
    cancel,
    create_hold,
    expire,
    hold,
)
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, connection, transaction

from database_urls import database_url_for_name
from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from patients.duplicate_support import _pair
from renewal.test_self_booking import _session
from scheduling.appointment_service_support import seed_appointment_setup
from scheduling.lifecycle_world import pinned_clock

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from apps.intake.models import DuplicateCandidate, MergeDecision
    from apps.intake.patient_creation import PatientRegistration

    from rbac_fixtures import RbacGraph


@pytest.fixture
def duplicate_booking_clock(superuser_database_url: str) -> Iterator[None]:
    """Keep every native hold live without depending on the test's wall time."""
    database_url = database_url_for_name(
        superuser_database_url, str(connection.settings_dict["NAME"])
    )
    with pinned_clock(database_url, datetime(2035, 6, 1, tzinfo=UTC)):
        yield


def _open_pair(
    graph: RbacGraph, state: Literal["requested", "held", "arrived"]
) -> tuple[PatientRegistration, PatientRegistration, DuplicateCandidate, Appointment]:
    services = importlib.import_module("apps.intake.services")
    first, second, candidate = _pair(graph, services)
    setup = replace(
        seed_appointment_setup(graph),
        enrollment_id=second.enrollment.pk,
        patient_id=second.patient.pk,
    )
    match state:
        case "requested":
            with (
                runtime_role(),
                tenant_context(graph.clinic_admin, graph.organization_a),
            ):
                publish_configuration(
                    clinic_id=graph.clinic_a,
                    expected_version=0,
                    content=ConfigurationContent(
                        display_name="Synthetic duplicate booking",
                        self_booking_requires_approval=True,
                    ),
                )
            patient_session = _session(setup)
            with runtime_role(), patient_session_context(patient_session):
                token = patient_slots(date(2035, 6, 2))[0].token
                appointment = book_patient_slot(token=token, idempotency_key=uuid4())
        case "held" | "arrived":
            with (
                runtime_role(),
                tenant_context(graph.shared_user, graph.organization_a),
            ):
                appointment = create_hold(
                    clinic_id=graph.clinic_a,
                    enrollment_id=second.enrollment.pk,
                    practitioner_id=graph.physician,
                    local_range=AppointmentLocalRange(
                        "2035-06-02T09:00", "2035-06-02T09:30"
                    ),
                    idempotency_key=uuid4(),
                )
                match state:
                    case "held":
                        assert appointment.status == "held"
                        return first, second, candidate, appointment
                    case "arrived":
                        appointment = book(
                            clinic_id=graph.clinic_a,
                            appointment_id=appointment.pk,
                            expected_revision=appointment.revision,
                            command_id=uuid4(),
                        )
                        appointment = arrive(
                            clinic_id=graph.clinic_a,
                            appointment_id=appointment.pk,
                            expected_revision=appointment.revision,
                            command_id=uuid4(),
                        )
                    case unreachable:
                        assert_never(unreachable)
        case unreachable:
            assert_never(unreachable)
    assert appointment.status == state
    return first, second, candidate, appointment


def _adjudicate(
    graph: RbacGraph, first: PatientRegistration, candidate: DuplicateCandidate
) -> MergeDecision:
    services = importlib.import_module("apps.intake.services")
    key = session_key(verified_client(graph))
    with (
        runtime_role(),
        tenant_context(graph.clinic_admin, graph.organization_a),
        services.duplicate_review_session(clinic_id=graph.clinic_a, session_key=key),
    ):
        return adjudicate_duplicate(
            clinic_id=graph.clinic_a,
            candidate_id=candidate.pk,
            decision="merge",
            survivor_patient_id=first.patient.pk,
            field_choices={},
            reason="Synthetic pending booking reviewed",
        )


@pytest.mark.parametrize("state", ["requested", "held", "arrived"])
@pytest.mark.usefixtures("duplicate_booking_clock")
def test_merge_reconciles_requested_and_live_held_bookings(
    rbac_graph: RbacGraph, state: Literal["requested", "held", "arrived"]
) -> None:
    # Given: a real patient request or native live hold, not a scheduled fixture.
    first, second, candidate, old = _open_pair(rbac_graph, state)
    original_binding = (old.patient_id, old.practitioner_id, old.start_at, old.end_at)
    # When: the human transfers this pending work through one real adjudication.
    decision = _adjudicate(rbac_graph, first, candidate)
    # Then: cancellation preserves the old binding and links a new unconfirmed hold.
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        old.refresh_from_db()
        assert old.status == "cancelled"
        assert (old.patient_id, old.practitioner_id, old.start_at, old.end_at) == (
            original_binding
        )
        assert old.patient_id == second.patient.pk
        link = PatientMergeTransfer.objects.get(
            merge_decision=decision, kind="appointment", original_id=str(old.pk)
        )
        successor = Appointment.objects.get(pk=link.replacement_id)
        assert successor.patient_id == first.patient.pk
        match state:
            case "requested" | "held":
                assert successor.status == "held"
                assert successor.hold_expires_at is not None
            case "arrived":
                assert successor.status == "scheduled"
                assert successor.hold_expires_at is None
            case unreachable:
                assert_never(unreachable)
        assert (successor.start_at, successor.end_at) == (old.start_at, old.end_at)
        assert (
            AppointmentTransition.objects.filter(
                appointment=old, from_status=state, to_status="cancelled"
            ).count()
            == 1
        )


def _retain_pending_row(*, clinic_id: UUID, merge_decision_id: UUID) -> None:
    """Arrange legacy unreconciled rows without replacing either defense under test."""


@pytest.mark.parametrize(
    ("state", "target"),
    [("requested", "held"), ("requested", "scheduled"), ("held", "scheduled")],
)
@pytest.mark.parametrize("boundary", ["service", "native"])
@pytest.mark.usefixtures("duplicate_booking_clock")
def test_alias_confirmation_refuses_unreconciled_pending_rows(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    state: Literal["requested", "held"],
    target: Literal["held", "scheduled"],
    boundary: Literal["service", "native"],
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: a retained pre-repair pending row. Only transfer is held; guards are real.
    first, _second, candidate, old = _open_pair(rbac_graph, state)
    monkeypatch.setattr(merge_execution, "transfer_scheduling", _retain_pending_row)
    _adjudicate(rbac_graph, first, candidate)
    command = uuid4()
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        # When: fresh booking authority tries to confirm that aliased identity.
        match boundary:
            case "service":
                match target:
                    case "held":
                        operation = hold
                    case "scheduled":
                        operation = book
                    case unreachable:
                        assert_never(unreachable)
                with pytest.raises(services.PatientAliasedError):
                    operation(
                        clinic_id=rbac_graph.clinic_a,
                        appointment_id=old.pk,
                        expected_revision=old.revision,
                        command_id=command,
                    )
            case "native":
                with pytest.raises(IntegrityError) as caught, transaction.atomic():
                    Appointment.objects.filter(pk=old.pk).update(
                        status=target, last_command_id=command
                    )
                cause = caught.value.__cause__
                assert isinstance(cause, psycopg.Error)
                assert cause.sqlstate == "23514"
                assert cause.diag.constraint_name == ("intake_patient_aliased_check")
            case unreachable:
                assert_never(unreachable)
        # Then: neither boundary confirmed it or emitted a transition receipt.
        old.refresh_from_db()
        assert old.status == state
        assert not AppointmentTransition.objects.filter(command_id=command).exists()


@pytest.mark.parametrize("state", ["requested", "held"])
@pytest.mark.usefixtures("duplicate_booking_clock")
def test_alias_pending_cancellation_remains_lawful_and_replayable(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    state: Literal["requested", "held"],
) -> None:
    # Given: the same retained legacy shape, with cancellation rather than new work.
    first, _second, candidate, old = _open_pair(rbac_graph, state)
    monkeypatch.setattr(merge_execution, "transfer_scheduling", _retain_pending_row)
    _adjudicate(rbac_graph, first, candidate)
    command = uuid4()
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        # When: an authorized actor cancels and replays that same command.
        cancelled = cancel(
            clinic_id=rbac_graph.clinic_a,
            appointment_id=old.pk,
            expected_revision=old.revision,
            command_id=command,
        )
        replay = cancel(
            clinic_id=rbac_graph.clinic_a,
            appointment_id=old.pk,
            expected_revision=old.revision,
            command_id=command,
        )
        # Then: the retained cancellation has one revision and one native receipt.
        assert cancelled.status == replay.status == "cancelled"
        assert cancelled.revision == replay.revision
        assert AppointmentTransition.objects.filter(command_id=command).count() == 1


@pytest.mark.parametrize(
    ("state", "target"),
    [("requested", "held"), ("requested", "scheduled"), ("held", "scheduled")],
)
@pytest.mark.usefixtures("duplicate_booking_clock")
def test_alias_booking_replays_a_pre_merge_confirmation(
    rbac_graph: RbacGraph,
    state: Literal["requested", "held"],
    target: Literal["held", "scheduled"],
) -> None:
    # Given: a confirmed command predates the merge and its lawful cancellation.
    first, _second, candidate, old = _open_pair(rbac_graph, state)
    command = uuid4()
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        match target:
            case "held":
                operation = hold
            case "scheduled":
                operation = book
            case unreachable:
                assert_never(unreachable)
        operation(
            clinic_id=rbac_graph.clinic_a,
            appointment_id=old.pk,
            expected_revision=old.revision,
            command_id=command,
        )
    _adjudicate(rbac_graph, first, candidate)
    with (
        runtime_role(),
        tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
    ):
        # When: the original confirmation command is replayed, not newly confirmed.
        replay = operation(
            clinic_id=rbac_graph.clinic_a,
            appointment_id=old.pk,
            expected_revision=old.revision,
            command_id=command,
        )
        # Then: its original now-cancelled identity and receipt are returned unchanged.
        assert replay.pk == old.pk
        assert replay.patient_id == old.patient_id
        assert replay.status == "cancelled"
        assert AppointmentTransition.objects.filter(command_id=command).count() == 1


@pytest.mark.usefixtures("duplicate_booking_clock")
def test_alias_due_hold_can_expire_at_the_exact_native_deadline(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    superuser_database_url: str,
) -> None:
    # Given: a retained live hold becomes an alias before its deadline.
    first, _second, candidate, old = _open_pair(rbac_graph, "held")
    monkeypatch.setattr(merge_execution, "transfer_scheduling", _retain_pending_row)
    _adjudicate(rbac_graph, first, candidate)
    assert old.hold_expires_at is not None
    database_url = database_url_for_name(
        superuser_database_url, str(connection.settings_dict["NAME"])
    )
    with pinned_clock(database_url, old.hold_expires_at), runtime_role():
        # When: W expires it exactly at the database-owned deadline.
        result = expire(
            clinic_id=rbac_graph.clinic_a,
            appointment_id=old.pk,
            expected_revision=old.revision,
            command_id=uuid4(),
        )
    # Then: native expiry is not blocked by a guard for genuinely new bookings.
    with owner_context(rbac_graph.organization_a):
        old.refresh_from_db()
        assert old.status == "expired"
        assert result.revision == old.revision
