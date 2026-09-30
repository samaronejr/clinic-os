"""Scope discovery, presentation flags, and authenticated request selectors."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.core import workspace
from apps.identity import current_context, otp_views, stepup_views
from apps.retention import services as retention
from apps.scheduling import (
    access,
    appointment_values,
    appointment_view,
    availability_creation,
    availability_presenter,
    patient_authority,
)
from apps.scheduling import (
    appointment_transition_state as transitions,
)
from django.contrib.auth.models import AnonymousUser
from django.urls import resolve

from identity.legacy_parity_support import LEGACY, MANAGERS, Boundary, has_rows

if TYPE_CHECKING:
    from identity.legacy_parity_support import LegacyWorld


def _agenda_scope(w: LegacyWorld, valid: bool) -> object:
    scope = access.authorized_view_scope(w.clinic_for(valid))
    assert scope.clinic.pk == w.clinic
    assert scope.practitioner_id == (w.actor.pk if w.role == "physician" else None)
    return scope


def _screen(w: LegacyWorld, valid: bool) -> object:
    screen = availability_presenter.authorized_screen(w.clinic_for(valid))
    assert screen.can_manage is (w.role in MANAGERS)
    assert bool(screen.choices) is (w.role in MANAGERS)
    return screen


def _request_user(w: LegacyWorld, valid: bool, *, active: bool) -> object:
    previous = w.request.user
    if not valid:
        w.request.user = AnonymousUser()
    try:
        return (
            stepup_views._current_active_user(w.request)
            if active
            else otp_views._current_user(w.request)
        )
    finally:
        w.request.user = previous


def _workspace(w: LegacyWorld, valid: bool) -> object:
    previous_user, previous_match = w.request.user, w.request.resolver_match
    w.request.resolver_match = resolve("/workspace/")
    if not valid:
        w.request.user = AnonymousUser()
    try:
        result = workspace.resolve_workspace(w.request)
        if valid:
            assert result is not None
            assert result.clinic is not None
            assert result.clinic.id == w.clinic
        return result
    finally:
        w.request.user, w.request.resolver_match = previous_user, previous_match


BOUNDARIES = (
    Boundary(
        "apps.scheduling.access.authorized_view_scope#projection",
        "projection",
        LEGACY,
        _agenda_scope,
    ),
    Boundary(
        "apps.scheduling.availability_presenter.authorized_screen",
        "projection",
        LEGACY,
        _screen,
    ),
    Boundary(
        "apps.scheduling.availability_presenter.manager_choices",
        "scope",
        MANAGERS,
        lambda w, ok: availability_presenter.manager_choices(w.clinic_for(ok)),
        has_rows,
    ),
    Boundary(
        "apps.scheduling.appointment_transition_state.discover_transition_appointment",
        "binding",
        LEGACY,
        lambda w, ok: transitions.discover_transition_appointment(
            w.appointment.pk if ok else uuid4()
        ),
    ),
    Boundary(
        "apps.scheduling.appointment_transition_state.reload_transition_appointment",
        "binding",
        LEGACY,
        lambda w, ok: transitions.reload_transition_appointment(
            transitions.transition_write_target(w.appointment),
            w.appointment.pk if ok else uuid4(),
        ),
    ),
    Boundary(
        "apps.core.workspace._visible_clinics",
        "membership",
        LEGACY,
        lambda w, ok: workspace._visible_clinics(w.actor.pk if ok else uuid4()),
        has_rows,
    ),
    Boundary(
        "apps.core.workspace.resolve_workspace",
        "projection",
        LEGACY,
        _workspace,
        has_rows,
    ),
    Boundary(
        "apps.identity.otp_views._current_user",
        "authentication",
        LEGACY,
        lambda w, ok: _request_user(w, ok, active=False),
        has_rows,
    ),
    Boundary(
        "apps.identity.stepup_views._current_active_user",
        "authentication",
        LEGACY,
        lambda w, ok: _request_user(w, ok, active=True),
        has_rows,
    ),
    Boundary(
        "apps.scheduling.appointment_view.view_appointment_for_transition",
        "scope",
        MANAGERS,
        lambda w, ok: appointment_view.view_appointment_for_transition(
            appointment_id=w.appointment.pk if ok else uuid4()
        ),
    ),
    Boundary(
        "apps.scheduling.availability_creation._require_active_physician",
        "scope",
        MANAGERS,
        lambda w, ok: availability_creation._require_active_physician(
            w.clinic_for(ok), w.graph.physician
        ),
    ),
    Boundary(
        "apps.scheduling.patient_authority.authorized_appointment_clinic",
        "scope",
        MANAGERS,
        lambda w, ok: patient_authority.authorized_appointment_clinic(w.clinic_for(ok)),
    ),
    Boundary(
        "apps.scheduling.appointment_values.require_active_practitioner",
        "scope",
        MANAGERS,
        lambda w, ok: appointment_values.require_active_practitioner(
            w.clinic_for(ok), w.graph.physician
        ),
    ),
    Boundary(
        "apps.retention.services._clinic",
        "scope",
        LEGACY,
        lambda w, ok: retention._clinic(w.clinic_for(ok)),
    ),
    Boundary(
        "apps.identity.current_context.practitioner_display_label",
        "projection",
        LEGACY,
        lambda w, ok: current_context.practitioner_display_label(
            w.actor.pk if ok else uuid4(), w.clinic
        ),
        lambda result: isinstance(result, str) and not _uuid_text(result),
    ),
)


def _uuid_text(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True
