"""Render the actual duplicate form, decision and reversal in Portuguese."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from django.urls import reverse
from django.utils.translation import gettext, override

from identity.lifecycle_test_support import verified_client
from patient_service_support import runtime_role
from patients.duplicate_support import _pair

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


def test_duplicate_review_and_reversal_render_translated_fields_and_summary(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.intake.services")
    # Given: the actual route and form, a real pair and a verified human session.
    first, _second, candidate = _pair(rbac_graph, services)
    client = verified_client(rbac_graph)
    url = reverse("intake:patient-duplicates", args=(rbac_graph.clinic_a,))
    with runtime_role(), override("pt-br"):
        # When: the real native workflow renders review, merge and reversal.
        reviewed = client.post(url, {"action": "review", "candidate_id": candidate.pk})
        assert reviewed.status_code == 200
        form = reviewed.context["form"]
        body = reviewed.content.decode()
        for field in form.fields.values():
            if not field.widget.is_hidden:
                assert str(field.label) in body
        for msgid in (
            "Legal name",
            "Social name",
            "Preferred name",
            "Sex at birth",
            "Gender identity",
            "Pronouns",
            "Preferred language",
            "Accessibility needs",
            "Occupation",
            "Not informed",
        ):
            assert gettext(msgid) != msgid
            assert gettext(msgid) in body
        payload = {
            **form.initial,
            "action": "merge",
            "survivor_patient_id": str(first.patient.pk),
            "reason": "Synthetic localized duplicate review",
            **{
                name: str(first.patient.pk)
                for name in form.fields
                if name.startswith("choice_")
            },
        }
        merged = client.post(url, payload)
        assert merged.status_code == 200
        review = merged.context["review"]
        body = merged.content.decode()
        for msgid in (
            "Appointments",
            "Encounters",
            "Charges",
            "Separate registrations",
            "Reason for the decision",
        ):
            assert gettext(msgid) != msgid
            assert gettext(msgid) in body
        assert (
            gettext(
                "Decision recorded. Historical records keep their "
                "original registration."
            )
            in body
        )
        rejected = client.post(
            url,
            {
                "action": "unmerge",
                "merge_decision_id": review.decision.pk,
                "reason": "",
            },
        )
        assert rejected.status_code == 200
        assert rejected.context["review"].decision.pk == review.decision.pk
        assert "reason" in rejected.context["unmerge_form"].errors
        assert 'id="unmerge-reason_error"' in rejected.content.decode()
        assert 'aria-invalid="true"' in rejected.content.decode()
        reversed_response = client.post(
            url,
            {
                "action": "unmerge",
                "merge_decision_id": review.decision.pk,
                "reason": "Synthetic localized reversal",
            },
        )
        # Then: feedback and retained-review controls use the compiled catalog too.
        assert reversed_response.status_code == 200
        assert (
            gettext(
                "Registrations separated. Later survivor records remain there "
                "and require review."
            )
            in reversed_response.content.decode()
        )
        assert gettext("Retained forward links and rows for review") in (
            reversed_response.content.decode()
        )
