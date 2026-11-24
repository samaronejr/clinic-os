"""Administrative and patient-independent real-source cases for read-boundary tests."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from apps.ai.models import AIArtifact, AIInvocation, CareProposal
from apps.comms import suggestions
from apps.comms.inbox_models import Conversation, SuggestedReply
from apps.comms.inbox_services import link_conversation_to_patient
from apps.consent.services import record_ai_disclosure
from apps.core.integration import execute_operation
from apps.intake.contacts import save_contact_destination, verify_contact
from django.urls import reverse
from django.utils.html import escape

from ai import care_support, revenue_support
from ai.chart_consent_support import accept_ai
from ai.pending_display_support import _inbox_pages, withdraw
from ai.pending_reader_probe import ReaderCase
from ai.test_care_views import _client
from ai.test_revenue_views import post as finance_post
from billing.journal_support import finance_scope
from comms.support import CONTACT, inbox_account, manager_of
from core.test_navigation import _post
from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.lifecycle_test_support import verified_client
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from ai.revenue_support import RevenueWorld
    from ai.support import GatewayWorld
    from rbac_fixtures import RbacGraph


def care_case(g: RbacGraph, patch: pytest.MonkeyPatch, secret: Path) -> ReaderCase:
    world = care_support.care_world(g, patch, secret)
    accept_ai(g, world.plan.enrollment_id)
    bundle = care_support.request_and_collect(world)
    assert bundle.invocation_id is not None
    keys = frozenset(str(row.pk) for row in bundle.items if row.state == "pending")
    assert len(keys) == 3
    client = _client(world.physician, "physician")
    url = reverse("ai:care-coordination", args=(g.clinic_a,))

    def visible(hx: bool) -> bool:
        client.defaults["HTTP_HX_REQUEST"] = "true" if hx else "false"
        response = _post(
            client,
            url,
            {"action": "refresh", "invocation_id": str(bundle.invocation_id)},
        )
        assert response.status_code in (200, 403)
        return any(key.encode() in response.content for key in keys)

    def refuse() -> None:
        with as_actor(g, g.physician):
            record_ai_disclosure(
                clinic_id=g.clinic_a,
                encounter_id=world.plan.encounter_id,
                informed=True,
                refused=True,
            )

    return ReaderCase(CareProposal._meta.db_table, keys, visible, refuse)


def revenue_case(world: RevenueWorld) -> ReaderCase:
    g = world.setup.graph
    patient, receipt = accept_ai(g, world.setup.appointment.enrollment_id)
    ready = revenue_support.request_and_collect(world)
    assert ready.invocation_id is not None
    item = next(item for item in ready.items if item.kind == "appeal.draft")
    with finance_scope(world.setup):
        key = AIInvocation.objects.get(pk=ready.invocation_id).output.pk
    client = verified_client(g)
    url = reverse("ai:revenue-cycle", args=(g.clinic_a,))
    with runtime_role(), fixed_otp_time():
        assert client.get(url).status_code == 200

    def visible(hx: bool) -> bool:
        client.defaults["HTTP_HX_REQUEST"] = "true" if hx else "false"
        page = finance_post(
            client,
            url,
            {"command": "refresh", "invocation_id": str(ready.invocation_id)},
        )
        assert page.status_code == 200
        return escape(item.text).encode() in page.content

    return ReaderCase(
        AIArtifact._meta.db_table,
        frozenset({str(key)}),
        visible,
        partial(withdraw, patient, receipt),
    )


def front_case(
    g: RbacGraph, patch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> ReaderCase:
    inbox = inbox_account(g, request.getfixturevalue("settings"))
    chart = chart_world(g)
    inbox.receive(inbox.message_body(text="Qual o horário?"))
    with owner_context(g.organization_a):
        conversation = Conversation.objects.get()
    actor = manager_of(g)
    patch.setattr(suggestions, "_DRAFTERS", [])
    answer = "Atendemos de segunda a sexta."
    suggestions.register_reply_drafter("synthetic", lambda _: answer)
    patient, receipt = accept_ai(g, chart.enrollment)
    with as_actor(g, actor):
        save_contact_destination(
            clinic_id=g.clinic_a,
            enrollment_id=chart.enrollment,
            channel="whatsapp",
            destination=f"+{CONTACT}",
            expected_version=None,
        )
        verify_contact(
            clinic_id=g.clinic_a,
            enrollment_id=chart.enrollment,
            channel="whatsapp",
            expected_version=1,
        )
        link_conversation_to_patient(
            clinic_id=g.clinic_a,
            conversation_id=conversation.pk,
            enrollment_id=chart.enrollment,
            expected_revision=conversation.revision,
        )
        key = suggestions.request_suggestion(
            clinic_id=g.clinic_a, conversation_id=conversation.pk
        )

    def visible(hx: bool) -> bool:
        native, fragment, resumed = _inbox_pages(g, conversation.pk)
        assert (escape(answer).encode() in resumed) == (
            escape(answer).encode() in native
        )
        return escape(answer).encode() in (fragment if hx else native)

    return ReaderCase(
        SuggestedReply._meta.db_table,
        frozenset({str(key)}),
        visible,
        partial(withdraw, patient, receipt),
    )


def analyst_case(world: GatewayWorld) -> ReaderCase:
    g = world.graph
    client = verified_client(replace(g, clinic_admin=world.administrator))
    url = reverse("analytics:reports", args=(g.clinic_a,))
    with runtime_role():
        asked = client.post(
            url,
            {
                "action": "ask",
                "question": "faltas",
                "metric": "no_show",
                "start": "01/11/2026",
                "end": "06/11/2026",
                "grouping": "day",
                "weekday": "0",
            },
        )
    assert asked.status_code == 200
    with owner_context(g.organization_a):
        invocation = AIInvocation.objects.get(purpose="analyst")
    assert invocation.operation_id is not None
    with runtime_role():
        assert execute_operation(operation_id=invocation.operation_id) == "succeeded"
    with owner_context(g.organization_a):
        key = invocation.output.pk

    def visible(hx: bool) -> bool:
        with runtime_role():
            page = client.post(
                url,
                {"action": "proposal", "reference": str(invocation.pk)},
                headers={"HX-Request": "true" if hx else "false"},
            )
        assert page.status_code == 200
        return b'data-analyst-state="review"' in page.content

    # This descriptor contains only a clinic aggregate query. There is no
    # represented patient and therefore no per-patient receipt to revoke.
    return ReaderCase(
        AIArtifact._meta.db_table,
        frozenset({str(key)}),
        visible,
        lambda: None,
        remains_visible=True,
    )
