"""Executable permission admissions found by the automatic Python census."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.core import patient_context, workspace_access
from apps.identity.models import UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.realtime import authorization, scopes
from django.urls import resolve, reverse

from identity.legacy_parity_support import Boundary

if TYPE_CHECKING:
    from identity.legacy_parity_support import LegacyWorld


def _roles(*permissions: str) -> tuple[str, ...]:
    return tuple(
        role
        for role in UserClinicRole.Role.values
        if set(permissions) & BUNDLES_V1[role]
    )


def _request(w: LegacyWorld, valid: bool) -> None:
    w.request.resolver_match = resolve(
        reverse("intake:patient-demographics", args=(w.clinic_for(valid),))
    )


def _billing(w: LegacyWorld, valid: bool) -> bool:
    _request(w, valid)
    return workspace_access._billing_surface_admitted(w.request, w.clinic_for(valid))


def _workspace(w: LegacyWorld, valid: bool) -> object:
    _request(w, valid)
    return workspace_access.workspace_denial(w.request)


BOUNDARIES = (
    Boundary(
        "apps.core.patient_context.patient_context_allowed",
        "workspace_admission",
        _roles("demographics.read"),
        lambda w, ok: patient_context.patient_context_allowed(
            clinic_id=w.clinic_for(ok)
        ),
    ),
    Boundary(
        "apps.core.workspace_access._billing_surface_admitted",
        "workspace_admission",
        _roles("demographics.billing_read"),
        _billing,
    ),
    Boundary(
        "apps.core.workspace_access.workspace_denial",
        "workspace_admission",
        _roles("demographics.read", "demographics.billing_read"),
        _workspace,
        lambda result: result is None,
    ),
    Boundary(
        "apps.realtime.authorization._staff_topics",
        "workspace_admission",
        _roles("appointment.read"),
        lambda w, ok: authorization._staff_topics(
            (f"clinic:{w.clinic_for(ok)}:agenda",)
        ),
    ),
    Boundary(
        "apps.realtime.scopes.register_job_topic",
        "workspace_admission",
        _roles("appointment.read"),
        lambda w, ok: scopes.register_job_topic(
            clinic_id=w.clinic_for(ok), permission="appointment.read"
        ),
    ),
)
