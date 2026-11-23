"""Submit the actual chart refresh form before and after its source visit closes."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.finalization import close_encounter
from apps.identity.models import CareTeamMembership
from apps.workflows import today
from django.urls import reverse
from django.utils import timezone

from ai.brief_support import seeded
from ehr.longitudinal_support import finalized, second_draft
from ehr.test_addenda import signed_in
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


class RefreshForms(HTMLParser):
    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
        self.current: dict[str, str] | None = None
        self.forms: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and values.get("action") == self.url:
            assert values.get("method") == "post"
            self.current = {}
        elif self.current is not None and tag in ("input", "button"):
            key, value = values.get("name"), values.get("value")
            if key is not None and value is not None:
                self.current[key] = value

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.current is not None:
            if self.current.get("command") == "display":
                self.forms.append(self.current)
            self.current = None


@pytest.mark.parametrize("htmx", [False, True], ids=["native", "htmx"])
def test_rendered_today_refresh_survives_original_visit_closure(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch, htmx: bool
) -> None:
    g = rbac_graph
    world = seeded(g, monkeypatch)
    encounter = world.chart.version.document.encounter
    with owner_context(g.organization_a):
        CareTeamMembership.objects.create(
            organization_id=g.organization_a,
            clinic_id=g.clinic_a,
            user_id=g.physician,
            patient_enrollment_id=world.chart.enrollment,
            role="physician",
            valid_from=timezone.now(),
        )
        appointment = encounter.appointment
        assert appointment is not None
        instant = appointment.start_at
    monkeypatch.setattr(today, "utc_now", lambda: instant)
    url = reverse("ai:chart-assistant", args=(g.clinic_a,))
    headers = {"HX-Request": "true"} if htmx else {}
    with signed_in(g.physician) as client:
        generated = client.post(
            url,
            {
                "command": "generate",
                "surface": "today",
                "enrollment_id": str(world.chart.enrollment),
            },
            headers=headers,
        )
        assert generated.status_code == 200
        parser = RefreshForms(url)
        parser.feed(generated.content.decode())
        (form,) = parser.forms
        assert form["surface"] == "today"
        assert form["encounter_id"] == str(encounter.pk)
        assert client.post(url, form, headers=headers).status_code == 200
    finalized(g, world.chart.version)
    with as_actor(g, g.physician):
        close_encounter(clinic_id=g.clinic_a, encounter_id=encounter.pk)
    wrong_visit = second_draft(g, world.chart, g.physician).document.encounter_id
    with runtime_role(), fixed_otp_time():
        denials = [
            client.post(url, body, headers=headers)
            for body in (
                {**form, "invocation_id": str(uuid4())},
                {**form, "encounter_id": str(wrong_visit)},
            )
        ]
        assert [response.status_code for response in denials] == [403, 403]
        assert denials[0].content == denials[1].content
        enrolled = {k: v for k, v in form.items() if k != "encounter_id"}
        control = client.post(
            url,
            {**enrolled, "enrollment_id": str(world.chart.enrollment)},
            headers=headers,
        )
        assert control.status_code == 200
        assert b'data-today-brief="unavailable"' in control.content
        refreshed = client.post(url, form, headers=headers)
        assert refreshed.status_code == control.status_code
        assert b'data-today-brief="unavailable"' in refreshed.content
