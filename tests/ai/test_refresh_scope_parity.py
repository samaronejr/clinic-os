"""The same stored turn has the same identity boundary on every chart surface."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai import brief_views
from apps.ai.brief_forms import ChartQuestionForm
from apps.ai.models import AIInvocation
from apps.ehr.chart_sources import chart_enrollment
from apps.ehr.finalization import close_encounter
from apps.identity.models import CareTeamMembership
from apps.tenancy.db import tenant_context
from apps.workflows.today import TodayAppointment, view_today
from django.forms import ChoiceField
from django.urls import reverse
from django.utils import timezone

from ai.brief_support import seeded
from ehr.longitudinal_support import finalized
from ehr.test_addenda import signed_in
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_actor
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("htmx", [False, True], ids=["native", "fragment"])
def test_refresh_on_every_surface_binds_the_stored_invocation_before_fallback(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, htmx: bool
) -> None:
    g = rbac_graph
    world = seeded(g, monkeypatch)
    with as_actor(g, g.physician):
        today = view_today(clinic_id=g.clinic_a)
    scheduled = TodayAppointment(
        uuid4(),
        timezone.now(),
        "12:00",
        "12:30",
        "Synthetic",
        "scheduled",
        encounter_open=False,
        can_start=True,
        enrollment_id=world.chart.enrollment,
    )
    monkeypatch.setattr(
        brief_views, "view_today", lambda **_: replace(today, next_patient=scheduled)
    )
    url = reverse("ai:chart-assistant", args=(g.clinic_a,))
    selection = {"enrollment_id": str(world.chart.enrollment)}
    headers = {"HX-Request": "true" if htmx else "false"}
    _reader, second = permission_actor(g, "physician")
    with owner_context(g.organization_a):
        # Open-encounter authority itself ends at closure. Give the clinician
        # genuine ongoing chart access to both patients, not to either turn.
        for enrollment in (world.chart.enrollment, second):
            CareTeamMembership.objects.create(
                organization_id=g.organization_a,
                clinic_id=g.clinic_a,
                patient_enrollment_id=enrollment,
                user_id=g.physician,
                role="physician",
                valid_from=timezone.now(),
            )
    with signed_in(g.physician) as client:
        generated = client.post(
            url,
            {**selection, "surface": "workspace", "command": "generate"},
            headers=headers,
        )
        assert generated.status_code == 200
        with tenant_context(g.physician, g.organization_a):
            own = AIInvocation.objects.get(clinic_id=g.clinic_a, user_id=g.physician)
        assert (
            client.post(
                url,
                {
                    **selection,
                    "surface": "workspace",
                    "command": "display",
                    "invocation_id": str(own.pk),
                },
                headers=headers,
            ).status_code
            == 200
        )
        previsit = client.post(
            url,
            {**selection, "surface": "today", "command": "generate"},
            headers=headers,
        )
        assert previsit.status_code == 200
        assert b"unavailable" in previsit.content
        unknown = str(uuid4())
        denials = []
        surfaces = ChartQuestionForm.base_fields["surface"]
        assert isinstance(surfaces, ChoiceField)
        choices = surfaces.choices
        assert isinstance(choices, (tuple, list))
        for surface, _label in choices:
            response = client.post(
                url,
                {
                    **selection,
                    "surface": surface,
                    "command": "display",
                    "invocation_id": unknown,
                },
                headers=headers,
            )
            assert response.status_code == 403, surface
            assert unknown.encode() not in response.content
            denials.append(response.content)
        # This is even the same patient, requester, and session. But this old
        # turn belongs to a different appointment than the next one on Today.
        wrong = client.post(
            url,
            {
                **selection,
                "surface": "today",
                "command": "display",
                "invocation_id": str(own.pk),
            },
            headers=headers,
        )
        assert wrong.status_code == 403
        assert all(body == wrong.content for body in denials)
        # A readable second patient is not an alternative scope for this turn.
        scheduled = replace(scheduled, enrollment_id=second)
        for surface, _label in choices:
            crossed = client.post(
                url,
                {
                    "enrollment_id": str(second),
                    "surface": surface,
                    "command": "display",
                    "invocation_id": str(own.pk),
                },
                headers=headers,
            )
            assert (crossed.status_code, crossed.content) == (403, wrong.content)
    # Same original visit/session: closing the real visit changes
    # availability, not identity. Both surfaces must preserve manual use.
    encounter = world.chart.version.document.encounter
    assert encounter.appointment_id is not None
    scheduled = replace(
        scheduled,
        appointment_id=encounter.appointment_id,
        enrollment_id=world.chart.enrollment,
    )
    finalized(g, world.chart.version)
    with as_actor(g, g.physician):
        close_encounter(clinic_id=g.clinic_a, encounter_id=encounter.pk)
        assert (
            chart_enrollment(clinic_id=g.clinic_a, enrollment_id=world.chart.enrollment)
            == world.chart.enrollment
        )
    with runtime_role(), fixed_otp_time():
        for surface, _label in choices:
            matched = client.post(
                url,
                {
                    **selection,
                    "surface": surface,
                    "command": "display",
                    "invocation_id": str(own.pk),
                },
                headers=headers,
            )
            assert matched.status_code == 200
            assert b"unavailable" in matched.content
