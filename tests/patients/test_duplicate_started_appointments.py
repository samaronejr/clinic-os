"""Started visits retain usable forward work through merge and separation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.services import open_encounter
from apps.intake import services
from apps.intake.models import PatientMergeTransfer
from apps.scheduling import patient_merge
from apps.scheduling.models import Appointment
from apps.scheduling.services import arrive
from apps.tenancy.db import tenant_context
from django.db import connection

from database_urls import database_url_for_name
from identity.lifecycle_test_support import session_key, verified_client
from patient_service_support import runtime_role
from patients.duplicate_booking_cases import (
    _adjudicate,
    _open_pair,
    duplicate_booking_clock,
)
from scheduling.lifecycle_world import pinned_clock

if TYPE_CHECKING:
    from datetime import datetime

    from rbac_fixtures import RbacGraph

__all__ = ("duplicate_booking_clock",)


@dataclass(frozen=True, slots=True)
class _MergeClock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("duplicate_booking_clock")
@pytest.mark.parametrize(
    "minutes_after_start", [0, 15], ids=["at-start", "after-start"]
)
def test_started_arrival_merge_preserves_a_usable_survivor_visit(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    minutes_after_start: int,
) -> None:
    # Given: arrival precedes the visit's start, and no encounter has been opened.
    first, second, candidate, old = _open_pair(rbac_graph, "arrived")
    original = (old.patient_id, old.practitioner_id, old.start_at, old.end_at)
    instant = old.start_at + timedelta(minutes=minutes_after_start)
    database_url = database_url_for_name(
        superuser_database_url, str(connection.settings_dict["NAME"])
    )
    with pinned_clock(database_url, instant), pytest.MonkeyPatch.context() as clock:
        clock.setattr(patient_merge, "timezone", _MergeClock(instant))
        # When: the verified human merges at or after the still-open visit's start.
        decision = _adjudicate(rbac_graph, first, candidate)
        # Then: the original is retained, terminal, and linked to usable survivor work.
        with (
            runtime_role(),
            tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        ):
            old.refresh_from_db()
            assert old.status == "cancelled"
            assert (old.patient_id, old.practitioner_id, old.start_at, old.end_at) == (
                original
            )
            link = PatientMergeTransfer.objects.get(
                merge_decision=decision, kind="appointment", original_id=str(old.pk)
            )
            successor = Appointment.objects.get(pk=link.replacement_id)
            assert successor.pk != old.pk
            assert successor.patient_id == first.patient.pk
            assert (successor.start_at, successor.end_at) == (old.start_at, old.end_at)
            assert not Appointment.objects.filter(
                clinic_id=rbac_graph.clinic_a,
                patient_id=second.patient.pk,
                status__in=("requested", "held", "scheduled", "arrived"),
            ).exists()
            successor = arrive(
                clinic_id=rbac_graph.clinic_a,
                appointment_id=successor.pk,
                expected_revision=successor.revision,
                command_id=uuid4(),
            )
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        ):
            encounter = open_encounter(
                clinic_id=rbac_graph.clinic_a, appointment_id=successor.pk
            )
            assert encounter.patient_id == first.patient.pk
            assert encounter.appointment_id == successor.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("duplicate_booking_clock")
@pytest.mark.parametrize(
    "minutes_after_start", [0, 15], ids=["at-start", "after-start"]
)
def test_started_arrival_unmerge_keeps_the_survivor_visit_and_original_history(
    rbac_graph: RbacGraph,
    superuser_database_url: str,
    minutes_after_start: int,
) -> None:
    # Given: the started arrival has a real survivor continuation and opened visit.
    first, second, candidate, old = _open_pair(rbac_graph, "arrived")
    original = (old.patient_id, old.practitioner_id, old.start_at, old.end_at)
    instant = old.start_at + timedelta(minutes=minutes_after_start)
    database_url = database_url_for_name(
        superuser_database_url, str(connection.settings_dict["NAME"])
    )
    with pinned_clock(database_url, instant), pytest.MonkeyPatch.context() as clock:
        clock.setattr(patient_merge, "timezone", _MergeClock(instant))
        decision = _adjudicate(rbac_graph, first, candidate)
        with (
            runtime_role(),
            tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
        ):
            old.refresh_from_db()
            assert old.status == "cancelled"
            link = PatientMergeTransfer.objects.get(
                merge_decision=decision, kind="appointment", original_id=str(old.pk)
            )
            successor = Appointment.objects.get(pk=link.replacement_id)
            successor = arrive(
                clinic_id=rbac_graph.clinic_a,
                appointment_id=successor.pk,
                expected_revision=successor.revision,
                command_id=uuid4(),
            )
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        ):
            encounter = open_encounter(
                clinic_id=rbac_graph.clinic_a, appointment_id=successor.pk
            )
            encounter_binding = (
                encounter.pk,
                encounter.patient_id,
                encounter.appointment_id,
                encounter.physician_id,
            )
        key = session_key(verified_client(rbac_graph))
        with (
            runtime_role(),
            tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
            services.duplicate_review_session(
                clinic_id=rbac_graph.clinic_a, session_key=key
            ),
        ):
            # When: the verified human separates the same identities again.
            alias = services.unmerge(
                clinic_id=rbac_graph.clinic_a,
                merge_decision_id=decision.pk,
                reason="Synthetic started visit separation",
            )
            # Then: views separate without moving either the source or later visit.
            assert not alias.active
            for registration in (first, second):
                assert services.patient_alias_set(
                    clinic_id=rbac_graph.clinic_a,
                    patient_id=registration.patient.pk,
                ).patient_ids == (registration.patient.pk,)
            old.refresh_from_db()
            successor.refresh_from_db()
            link.refresh_from_db()
            assert old.status == "cancelled"
            assert (old.patient_id, old.practitioner_id, old.start_at, old.end_at) == (
                original
            )
            assert successor.patient_id == first.patient.pk
            assert (link.original_id, link.replacement_id) == (
                str(old.pk),
                str(successor.pk),
            )
        with (
            runtime_role(),
            tenant_context(rbac_graph.physician, rbac_graph.organization_a),
        ):
            reopened = open_encounter(
                clinic_id=rbac_graph.clinic_a, appointment_id=successor.pk
            )
            assert (
                reopened.pk,
                reopened.patient_id,
                reopened.appointment_id,
                reopened.physician_id,
            ) == encounter_binding
