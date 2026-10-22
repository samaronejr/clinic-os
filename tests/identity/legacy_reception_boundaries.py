"""Reception authorization at actual queue, room and device boundaries."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.scheduling import reception, reception_queries, reception_views
from django.test import RequestFactory

from identity.legacy_parity_support import Boundary, http_allowed
from identity.reception_subjects import ReceptionSubjects, seed_reception

if TYPE_CHECKING:
    from django.http import HttpRequest

    from identity.legacy_parity_support import LegacyWorld

QUEUE_ROLES = ("receptionist", "nurse")
RECEPTION_ROLES = ("receptionist",)
READINESS_ROLES = tuple(
    role
    for role in UserClinicRole.Role.values
    if "appointment.move" in BUNDLES_V1[role] or role == "physician"
)


def reception_world(subject: LegacyWorld) -> LegacyWorld:
    return replace(
        subject, reception=seed_reception(subject.graph, subject.appointment)
    )


def _subjects(subject: LegacyWorld) -> ReceptionSubjects:
    assert subject.reception is not None
    return subject.reception


def _request(subject: LegacyWorld) -> HttpRequest:
    request = RequestFactory().get("/workspace/")
    request.user = subject.request.user
    request.session = subject.request.session
    return request


def _display(subject: LegacyWorld, valid: bool) -> bool:
    request = _request(subject)
    request.COOKIES[reception_views.DEVICE_COOKIE] = (
        _subjects(subject).device.token if valid else "invalid"
    )
    return http_allowed(reception_views.waiting_display(request))


BOUNDARIES = (
    Boundary(
        "apps.scheduling.reception.reception_clinic",
        "reception",
        QUEUE_ROLES,
        lambda w, ok: reception.reception_clinic(clinic_id=w.clinic_for(ok)),
    ),
    Boundary(
        "apps.scheduling.reception.reception_admin_clinic",
        "reception",
        RECEPTION_ROLES,
        lambda w, ok: reception.reception_admin_clinic(clinic_id=w.clinic_for(ok)),
    ),
    Boundary(
        "apps.scheduling.reception.arrival_readiness",
        "reception",
        READINESS_ROLES,
        lambda w, ok: reception.arrival_readiness(
            clinic_id=w.clinic_for(ok),
            appointment_id=_subjects(w).setup.appointment.pk,
        ),
    ),
    Boundary(
        "apps.scheduling.reception.mark_arrived",
        "reception",
        READINESS_ROLES,
        lambda w, ok: reception.mark_arrived(
            clinic_id=w.clinic_for(ok),
            appointment_id=_subjects(w).setup.appointment.pk,
            location_id=_subjects(w).setup.location.pk,
            expected_revision=_subjects(w).setup.appointment.revision,
            command_id=uuid4(),
        ),
    ),
    Boundary(
        "apps.scheduling.reception.call_to_room",
        "reception",
        QUEUE_ROLES,
        lambda w, ok: reception.call_to_room(
            clinic_id=w.clinic_for(ok),
            queue_entry_id=_subjects(w).queue.pk,
            room_id=_subjects(w).setup.room.pk,
            room_unit=1,
            expected_revision=_subjects(w).queue.revision,
        ),
    ),
    Boundary(
        "apps.scheduling.reception.update_room_status",
        "reception",
        QUEUE_ROLES,
        lambda w, ok: reception.update_room_status(
            clinic_id=w.clinic_for(ok),
            room_id=_subjects(w).setup.room.pk,
            unit=1,
            state="available",
            expected_revision=0,
        ),
    ),
    Boundary(
        "apps.scheduling.reception.broadcast_clinician_delay",
        "reception",
        RECEPTION_ROLES,
        lambda w, ok: reception.broadcast_clinician_delay(
            clinic_id=w.clinic_for(ok),
            practitioner_id=_subjects(w).setup.booking.practitioner_id,
            minutes=5,
            expected_revision=0,
        ),
    ),
    Boundary(
        "apps.scheduling.reception.issue_display_device",
        "reception",
        RECEPTION_ROLES,
        lambda w, ok: reception.issue_display_device(clinic_id=w.clinic_for(ok)),
    ),
    Boundary(
        "apps.scheduling.reception.revoke_display_device",
        "reception",
        RECEPTION_ROLES,
        lambda w, ok: reception.revoke_display_device(
            clinic_id=w.clinic_for(ok),
            device_id=_subjects(w).device.device_id,
        ),
    ),
    Boundary(
        "apps.scheduling.reception_queries.reception_practitioners",
        "reception",
        QUEUE_ROLES,
        lambda w, ok: reception_queries.reception_practitioners(
            clinic_id=w.clinic_for(ok)
        ),
    ),
    Boundary(
        "apps.scheduling.reception_queries.view_reception_day",
        "reception",
        QUEUE_ROLES,
        lambda w, ok: reception_queries.view_reception_day(
            clinic_id=w.clinic_for(ok),
            day=_subjects(w).setup.appointment.start_at.date(),
        ),
    ),
    Boundary(
        "apps.scheduling.reception_views._render",
        "reception",
        QUEUE_ROLES,
        lambda w, ok: http_allowed(
            reception_views._render(
                _request(w),
                w.clinic_for(ok),
                day=_subjects(w).setup.appointment.start_at.date(),
            )
        ),
    ),
    Boundary(
        "apps.scheduling.reception_views.reception_view",
        "reception",
        QUEUE_ROLES,
        lambda w, ok: http_allowed(
            reception_views.reception_view(
                _request(w),
                clinic_id=w.clinic_for(ok),
            )
        ),
    ),
    Boundary(
        "apps.scheduling.reception_views.waiting_display",
        "reception",
        tuple(UserClinicRole.Role.values),
        _display,
    ),
)
