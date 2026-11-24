"""Native read oracles for patient-linked non-chart proposals."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.care_coordination import CareAccessDeniedError, care_bundle
from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError
from apps.ai.extraction import collect_extraction
from apps.comms import suggestions
from apps.comms.inbox_queries import conversation_view
from apps.comms.inbox_services import link_conversation_to_patient
from apps.comms.inbox_views import REOPEN_SESSION_KEY
from apps.comms.models import Conversation
from apps.consent.services import revoke_consent
from apps.intake.contacts import save_contact_destination, verify_contact
from apps.intake.patient_access import patient_session_context
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from ai import care_support, revenue_support
from ai.chart_consent_support import accept_ai
from ai.test_care_views import _client
from ai.test_extraction_runtime import extracted_job
from ai.test_revenue_cycle import proposal_for
from ai.test_revenue_views import post as finance_post
from billing.journal_support import finance_scope
from comms.support import CONTACT, inbox_account, manager_of
from core.test_navigation import _get, _post
from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.lifecycle_test_support import verified_client
from identity.permission_support import owner_context
from orders.support import ordered
from otp_test_support import fixed_otp_time
from patient_http_support import receptionist_client
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph


def withdraw(patient: UUID, receipt: UUID) -> None:
    with runtime_role(), patient_session_context(patient):
        revoke_consent(acceptance_id=receipt)


def _inbox_pages(g: RbacGraph, thread: UUID) -> tuple[bytes, ...]:
    """Read the selected thread through the native, fragment and resume routes."""
    client, _actor = receptionist_client(g)
    url = reverse("comms:inbox", args=(g.clinic_a,))
    form = {"action": "show", "conversation_id": str(thread)}
    with runtime_role():
        native = client.post(url, form)
        fragment = client.post(url, form, headers={"HX-Request": "true"})
        session = client.session
        session[REOPEN_SESSION_KEY] = str(thread)
        session.save()
        resumed = client.get(url)
    pages = (native, fragment, resumed)
    assert all(page.status_code == 200 for page in pages)
    return tuple(page.content for page in pages)


def front_desk(
    g: RbacGraph, patch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    inbox = inbox_account(g, request.getfixturevalue("settings"))
    chart = chart_world(g)
    inbox.receive(inbox.message_body(text="Qual o horário?"))
    with owner_context(g.organization_a):
        conversation = Conversation.objects.get()
    actor = manager_of(g)
    # The front-desk reply is a real stored, pending draft; only its synthetic
    # text generator is supplied, as in the inbox's native suite.
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
        before = conversation_view(
            clinic_id=g.clinic_a, conversation_id=conversation.pk, now=timezone.now()
        )
        assert before.suggestion is not None
        assert (before.suggestion.pk, before.suggestion.body) == (key, answer)
    assert all(
        escape(answer).encode() in page for page in _inbox_pages(g, conversation.pk)
    )
    withdraw(patient, receipt)
    assert all(
        escape(answer).encode() not in page for page in _inbox_pages(g, conversation.pk)
    )
    with as_actor(g, actor):
        after = conversation_view(
            clinic_id=g.clinic_a, conversation_id=conversation.pk, now=timezone.now()
        )
        assert after.suggestion is None
        assert before.suggestion is None
        assert after.messages
        assert after.free_text


def finance(
    g: RbacGraph, patch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    settings = request.getfixturevalue("settings")
    settings.INSURANCE_SYNTHETIC_ENABLED = True
    world = revenue_support.revenue_world(
        request.getfixturevalue("finance_setup"),
        patch,
        request.getfixturevalue("synthetic_secret_backend"),
    )
    with finance_scope(world.setup):
        enrolled = world.cycle.guide.membership.enrollment_id
    patient, receipt = accept_ai(g, enrolled)
    ready = revenue_support.request_and_collect(world)
    assert ready.invocation_id is not None
    item = next(i for i in ready.items if i.kind == "appeal.draft")
    client = verified_client(world.setup.graph)
    url = reverse("ai:revenue-cycle", args=(g.clinic_a,))
    with runtime_role(), fixed_otp_time():
        assert client.get(url).status_code == 200
    body = {"command": "refresh", "invocation_id": str(ready.invocation_id)}
    before = finance_post(client, url, body)
    assert before.status_code == 200
    assert escape(item.text).encode() in before.content
    withdraw(patient, receipt)
    after = finance_post(client, url, body)
    assert after.status_code == 200
    assert after.context["review"].state == "stale"
    assert escape(item.text).encode() not in after.content


def test_finance_queue(world: revenue_support.RevenueWorld) -> None:
    """A validated appeal proposal is pending even on the generic agent queue."""
    setup = world.setup
    revenue_support.add_clinic_manager_role(setup)
    patient, receipt = accept_ai(setup.graph, setup.appointment.enrollment_id)
    review = revenue_support.request_and_collect(world)
    with finance_scope(setup):
        proposal = proposal_for(world, review)
    client = verified_client(setup.graph)
    url = reverse("ai:agents", args=(setup.clinic_id,))
    with runtime_role(), fixed_otp_time():
        before = client.get(url)
    assert before.status_code == 200
    assert str(proposal.pk).encode() in before.content
    withdraw(patient, receipt)
    with runtime_role(), fixed_otp_time():
        after = client.get(url)
    assert after.status_code == 200
    assert str(proposal.pk).encode() not in after.content


def extraction(g: RbacGraph, patch: pytest.MonkeyPatch) -> None:
    source = ordered(g)
    patient, receipt = accept_ai(g, source.enrollment)
    job = extracted_job(g, patch, world=source)
    physician = _client(g.physician, "physician")
    url = reverse("ai:extraction", args=(g.clinic_a,))
    form = {"action": "refresh", "job_id": str(job.pk)}
    with as_actor(g, g.physician):
        candidate = (
            collect_extraction(clinic_id=g.clinic_a, job_id=job.pk)
            .candidates.order_by("position")
            .first()
        )
        assert candidate is not None
    before = _post(physician, url, form)
    assert before.status_code == 200
    assert str(candidate.pk).encode() in before.content
    withdraw(patient, receipt)
    after = _post(physician, url, form)
    assert after.status_code in (403, 409)
    assert str(candidate.pk).encode() not in after.content
    with (
        as_actor(g, g.physician),
        pytest.raises((GatewayAccessDeniedError, GatewayRefusedError)),
    ):
        collect_extraction(clinic_id=g.clinic_a, job_id=job.pk)


def care(
    g: RbacGraph, patch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    world = care_support.care_world(
        g, patch, request.getfixturevalue("synthetic_secret_backend")
    )
    patient, receipt = accept_ai(g, world.plan.enrollment_id)
    bundle = care_support.request_and_collect(world)
    assert bundle.invocation_id is not None
    assert bundle.items
    manager = _client(world.manager, "clinic_manager")
    physician = _client(world.physician, "physician")
    url = reverse("ai:care-coordination", args=(g.clinic_a,))
    selector = {"invocation_id": str(bundle.invocation_id)}
    for client in (manager, physician):
        before = _get(client, url)
        assert before.status_code == 200
        assert str(bundle.invocation_id).encode() in before.content
    preview = _post(manager, url, {"action": "preview", **selector})
    assert preview.status_code == 200
    assert b"data-care-preview-item=" in preview.content
    withdraw(patient, receipt)
    for client in (manager, physician):
        after = _get(client, url)
        assert after.status_code == 200
        assert str(bundle.invocation_id).encode() not in after.content
    for client, action in ((manager, "preview"), (physician, "refresh")):
        refused = _post(client, url, {"action": action, **selector})
        assert refused.status_code == 403
        assert b"data-care-preview-item=" not in refused.content
        assert b"data-care-item=" not in refused.content
    for actor in (world.physician, world.manager):
        with care_support.acting(g, actor):
            try:
                panel = care_bundle(
                    clinic_id=g.clinic_a, invocation_id=bundle.invocation_id
                )
            except CareAccessDeniedError:
                continue
            assert not panel.items
