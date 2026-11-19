"""A grant can start or expire between clinic discovery and shell rendering."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest
from apps.core import patient_context, workspace
from apps.ehr.models import Encounter
from apps.identity.models import Clinic, User, UserClinicRole
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.test import RequestFactory
from django.urls import resolve, reverse

if TYPE_CHECKING:
    from uuid import UUID


@pytest.mark.parametrize(
    "before",
    [
        {"demographics.read", "demographics.write"},
        set(),
    ],
    ids=("write-narrowed", "read-restored"),
)
def test_banner_uses_later_grants_on_a_clinic_switch(
    monkeypatch: pytest.MonkeyPatch,
    before: set[str],
) -> None:
    actor = User(username="synthetic-grant-freshness")
    old = Clinic(name="A Sintetica", timezone="UTC")
    clinic = Clinic(name="B Sintetica", timezone="UTC")
    role = UserClinicRole.Role.RECEPTIONIST
    assignments = Mock()
    assignments.select_related.return_value.order_by.return_value = [
        UserClinicRole(user=actor, clinic=unit, role=role) for unit in (old, clinic)
    ]
    monkeypatch.setattr(
        UserClinicRole.objects, "filter", Mock(return_value=assignments)
    )

    common = frozenset({"appointment.read", "appointment.book"})
    snapshots = iter((common | before, common | {"demographics.read"}))

    def grants(clinic_id: UUID) -> frozenset[str]:
        assert clinic_id in {old.pk, clinic.pk}
        return next(snapshots) if clinic_id == clinic.pk else common

    monkeypatch.setattr(workspace, "granted_permissions", grants)
    enrollment = uuid4()
    key = f"{patient_context.SESSION_PREFIX}{clinic.pk}"
    url = reverse("scheduling:agenda-board", args=(clinic.pk, "day", "2035-06-02"))
    request = RequestFactory().get(url)
    request.resolver_match = resolve(url)
    request.user = actor
    request.session = SessionStore()
    request.session[workspace.ACTIVE_CLINIC_SESSION_KEY] = str(old.pk)
    request.session[key] = str(enrollment)

    monkeypatch.setattr(
        patient_context, "patient_context_allowed", Mock(return_value=True)
    )
    monkeypatch.setattr(
        Encounter.objects,
        "filter",
        Mock(return_value=Mock(**{"exists.return_value": False})),
    )
    monkeypatch.setattr(
        patient_context,
        "patient_alias_set",
        Mock(return_value=SimpleNamespace(unified_at=None)),
    )
    profile = Mock(
        return_value=SimpleNamespace(
            enrollment_id=enrollment,
            patient_id=uuid4(),
            values={},
            display_name="Pessoa Sintetica",
            birth_date=None,
            identifiers=(),
        )
    )
    monkeypatch.setattr(patient_context, "demographics_profile", profile)

    shell = workspace.resolve_workspace(request)
    assert shell is not None
    assert shell.clinic is not None
    assert shell.patient is not None
    assert shell.clinic.id == clinic.pk
    assert tuple(action.key for action in shell.patient.actions) == ("book",)
    profile.assert_called_once_with(
        clinic_id=clinic.pk, enrollment_id=enrollment, record_view=True
    )
    assert request.session[key] == str(enrollment)
    assert request.session[workspace.ACTIVE_CLINIC_SESSION_KEY] == str(old.pk)
    workspace.finish_clinic_selection(request, accepted=True)
    assert request.session[workspace.ACTIVE_CLINIC_SESSION_KEY] == str(clinic.pk)
