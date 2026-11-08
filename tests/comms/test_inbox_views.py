"""Staff entry points of the inbox on real PostgreSQL: pages, link, start, replies."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.comms import inbox_services as services
from apps.comms.models import Conversation, Message, QuickReply
from apps.identity.models import CareTeamMembership
from apps.intake.contacts import save_contact_destination, verify_contact
from apps.intake.models import PatientClinicEnrollment
from django.utils import timezone
from django.utils.translation import gettext, pgettext

from comms.support import CONTACT, InboxWorld, inbox_account, manager_of
from core.test_navigation import _client_for
from core.test_navigation import _post as _nav_post
from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_http_support import receptionist_client, verified_physician_client
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from django.test import Client
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
FOUND = 302
NOT_FOUND = 404
OK = 200


@pytest.fixture
def world(rbac_graph: RbacGraph, settings: SettingsWrapper) -> InboxWorld:
    return inbox_account(rbac_graph, settings)


@pytest.fixture(autouse=True)
def clinic_manager(rbac_graph: RbacGraph) -> UUID:
    return manager_of(rbac_graph)


def _inbox(graph: RbacGraph) -> str:
    return f"/clinics/{graph.clinic_a}/messages/"


def _thread(graph: RbacGraph) -> tuple[Conversation, str]:
    with owner_context(graph.organization_a):
        conversation = Conversation.objects.get()
    return conversation, f"{_inbox(graph)}{conversation.pk}/"


def _get(client: Client, url: str) -> str:
    with runtime_role():
        response = client.get(url)
    assert response.status_code == OK
    return response.content.decode()


def _post(client: Client, url: str, data: dict[str, object], *, status: int) -> str:
    with runtime_role():
        response = client.post(url, data)
    assert response.status_code == status
    return response.content.decode()


def _patient(graph: RbacGraph, *, verified: bool) -> tuple[UUID, str]:
    """A charted patient whose WhatsApp contact is the inbound number."""
    chart = chart_world(graph)
    with as_actor(graph, manager_of(graph)):
        save_contact_destination(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            channel="whatsapp",
            destination=f"+{CONTACT}",
            expected_version=None,
        )
        if verified:
            verify_contact(
                clinic_id=graph.clinic_a,
                enrollment_id=chart.enrollment,
                channel="whatsapp",
                expected_version=1,
            )
        name = str(
            PatientClinicEnrollment.objects.get(pk=chart.enrollment).patient.full_name
        )
    return chart.enrollment, name


def test_both_pages_subscribe_to_the_messages_topic_and_refetch_their_region(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    client, _ = receptionist_client(graph)
    _, thread = _thread(graph)
    topic = f'data-realtime-topic="clinic:{graph.clinic_a}:messages"'
    trigger = 'hx-trigger="rt:messages from:body, rt:poll from:body"'
    for url, region in (
        (_inbox(graph), "conversation-list"),
        (thread, "thread-region"),
    ):
        html = _get(client, url)
        assert topic in html
        assert f'id="{region}"' in html
        assert trigger in html
        # The element that carries the trigger stays put and only its children
        # are swapped: htmx binds a swapped-in element's trigger after its settle
        # delay, and a hint that lands in that gap would be lost.
        assert f'hx-select="#{region} &gt; *" hx-swap="innerHTML"' in html
        assert "data-realtime-unavailable" in html
    # The composer sits outside the refreshed region, so typed text survives.
    html = _get(client, thread)
    assert html.index('name="body"') > html.index('hx-select="#thread-region &gt; *"')
    assert html.index("</div>", html.index('id="thread-region"')) < html.index(
        'name="body"'
    )


def test_page_copy_comes_from_the_catalog(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    client, _ = receptionist_client(graph)
    _, thread = _thread(graph)
    html = _get(client, thread)
    for msgid in ("Service window", "Internal notes", "Link to a patient"):
        assert gettext(msgid) in html
        assert gettext(msgid) != msgid
        assert f">{msgid}<" not in html
    assert pgettext("inbox", "Send") in html
    assert pgettext("delivery state", "Queued") not in html
    listing = _get(client, _inbox(graph))
    assert gettext("Start a conversation") in listing
    assert pgettext("inbox filter", "Pending") in listing


def test_reception_finds_a_patient_and_links_the_verified_number(
    world: InboxWorld,
) -> None:
    graph = world.graph
    enrollment, name = _patient(graph, verified=True)
    world.receive(world.message_body(text="Qual o horário?"))
    conversation, thread = _thread(graph)
    client, _ = receptionist_client(graph)
    found = _post(client, thread, {"action": "find_patient", "q": name}, status=OK)
    assert name in found
    assert f'name="enrollment_id" value="{enrollment}"' in found
    assert gettext("Check or verify the contact") in found
    link = {
        "action": "link",
        "enrollment_id": str(enrollment),
        "expected_revision": conversation.revision,
    }
    with runtime_role():
        redirected = client.post(thread, link)
    assert redirected.status_code == FOUND
    page = _get(client, thread)
    assert gettext("Patient linked through a verified contact.") in page
    assert _thread(graph)[0].enrollment_id == enrollment


def test_an_unverified_contact_is_refused_and_offers_verification(
    world: InboxWorld,
) -> None:
    graph = world.graph
    enrollment, name = _patient(graph, verified=False)
    world.receive(world.message_body(text="Qual o horário?"))
    conversation, thread = _thread(graph)
    client, _ = receptionist_client(graph)
    found = _post(client, thread, {"action": "find_patient", "q": name}, status=OK)
    assert gettext("Check or verify the contact") in found
    with runtime_role():
        client.post(
            thread,
            {
                "action": "link",
                "enrollment_id": str(enrollment),
                "expected_revision": conversation.revision,
            },
        )
    page = _get(client, thread)
    assert gettext("The patient's contact is not verified.") in page
    assert _thread(graph)[0].enrollment_id is None


def test_the_patient_lookup_names_its_failures(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    _, thread = _thread(graph)
    client, _ = receptionist_client(graph)
    short = _post(client, thread, {"action": "find_patient", "q": "x"}, status=OK)
    assert gettext("Type at least two letters of the patient's full name.") in short
    none = _post(
        client, thread, {"action": "find_patient", "q": "Ninguem Sintetico"}, status=OK
    )
    assert gettext("No patient found for that name.") in none


def test_a_role_without_registry_search_is_told_to_ask_reception(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    _, thread = _thread(graph)
    # A scheduler handles the inbox but holds no registry search.
    client, _ = _client_for(graph, "scheduler")
    refused = _nav_post(client, thread, {"action": "find_patient", "q": "Sintetico"})
    assert refused.status_code == OK
    assert (
        gettext(
            "Your role cannot search patients here. Ask reception to link this number."
        )
        in refused.content.decode()
    )


def test_start_a_conversation_from_a_found_patient(world: InboxWorld) -> None:
    graph = world.graph
    enrollment, name = _patient(graph, verified=True)
    client, _ = receptionist_client(graph)
    url = _inbox(graph)
    found = _post(client, url, {"action": "find_patient", "q": name}, status=OK)
    assert name in found
    assert gettext("Start conversation") in found
    with runtime_role():
        started = client.post(
            url,
            {
                "action": "open",
                "enrollment_id": str(enrollment),
                "channel": "whatsapp",
                "purpose": "transactional",
            },
        )
    assert started.status_code == FOUND
    conversation, _ = _thread(graph)
    assert (conversation.purpose, conversation.enrollment_id) == (
        "transactional",
        enrollment,
    )
    assert started["Location"].endswith(f"{conversation.pk}/")
    # The same start again returns the live thread instead of a second one.
    with runtime_role():
        again = client.post(
            url,
            {
                "action": "open",
                "enrollment_id": str(enrollment),
                "channel": "whatsapp",
                "purpose": "transactional",
            },
        )
    assert again.status_code == FOUND
    with owner_context(graph.organization_a):
        assert Conversation.objects.count() == 1


def test_a_start_for_an_unknown_patient_or_action_changes_nothing(
    world: InboxWorld,
) -> None:
    graph = world.graph
    client, _ = receptionist_client(graph)
    url = _inbox(graph)
    _post(
        client,
        url,
        {
            "action": "open",
            "enrollment_id": str(uuid4()),
            "channel": "whatsapp",
            "purpose": "transactional",
        },
        status=NOT_FOUND,
    )
    with runtime_role():
        refused = client.post(url, {"action": "nonsense"})
    assert refused.status_code == OK
    assert gettext("Review the text or the template parameters.") in (
        refused.content.decode()
    )
    with owner_context(graph.organization_a):
        assert Conversation.objects.count() == 0


def test_a_refused_start_returns_to_the_list_with_the_reason(
    world: InboxWorld,
) -> None:
    graph = world.graph
    enrollment, _ = _patient(graph, verified=True)
    client, _ = receptionist_client(graph)
    with runtime_role():
        refused = client.post(
            _inbox(graph),
            {
                "action": "open",
                "enrollment_id": str(enrollment),
                "channel": "carrier-pigeon",
                "purpose": "transactional",
            },
        )
    assert refused.status_code == FOUND
    page = _get(client, _inbox(graph))
    assert gettext("This channel and purpose cannot start a conversation.") in page


def test_a_clinician_starts_a_care_thread_from_the_care_team(
    world: InboxWorld,
) -> None:
    graph = world.graph
    enrollment, name = _patient(graph, verified=True)
    with owner_context(graph.organization_a):
        CareTeamMembership.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient_enrollment_id=enrollment,
            user_id=graph.physician,
            role="physician",
            valid_from=timezone.now() - timedelta(days=1),
        )
    client = verified_physician_client(graph)
    url = _inbox(graph)
    found = _post(client, url, {"action": "find_patient", "q": ""}, status=OK)
    assert name in found
    assert pgettext("conversation purpose", "Care") in found
    narrowed = _post(client, url, {"action": "find_patient", "q": "zzz"}, status=OK)
    assert name not in narrowed
    with runtime_role():
        started = client.post(
            url,
            {
                "action": "open",
                "enrollment_id": str(enrollment),
                "channel": "whatsapp",
                "purpose": "care",
            },
        )
    assert started.status_code == FOUND
    conversation, thread = _thread(graph)
    assert (conversation.purpose, conversation.enrollment_id) == ("care", enrollment)
    # Reception cannot open the care thread the clinician started.
    reception, _ = receptionist_client(graph)
    with runtime_role():
        assert reception.get(thread).status_code == NOT_FOUND
    assert name in _get(client, thread)


def test_a_clinician_without_a_care_team_sees_no_patients(world: InboxWorld) -> None:
    graph = world.graph
    _patient(graph, verified=True)
    client = verified_physician_client(graph)
    found = _post(client, _inbox(graph), {"action": "find_patient", "q": ""}, status=OK)
    assert gettext("No patient found for that name.") in found


def test_quick_replies_fill_the_composer_and_send_nothing(world: InboxWorld) -> None:
    graph = world.graph
    with as_actor(graph, manager_of(graph)):
        services.create_quick_reply(
            clinic_id=graph.clinic_a,
            title="Horário",
            body="Atendemos das 8h às 18h.",
            purpose="transactional",
        )
    world.receive(world.message_body(text="Qual o horário?"))
    _, thread = _thread(graph)
    client, _ = receptionist_client(graph)
    page = _get(client, thread)
    assert "data-quick-replies" in page
    assert "Horário" in page
    with owner_context(graph.organization_a):
        reply = QuickReply.objects.get()
    filled = _post(
        client,
        thread,
        {"action": "quick_reply", "quick_reply_id": str(reply.pk)},
        status=OK,
    )
    assert ">Atendemos das 8h às 18h.</textarea>" in filled
    foreign = _post(
        client,
        thread,
        {"action": "quick_reply", "quick_reply_id": str(uuid4())},
        status=OK,
    )
    assert "Atendemos" not in foreign
    with owner_context(graph.organization_a):
        assert not Message.objects.filter(direction="out").exists()


def test_quick_replies_are_hidden_when_free_text_is_not_allowed(
    world: InboxWorld,
) -> None:
    graph = world.graph
    with as_actor(graph, manager_of(graph)):
        services.create_quick_reply(
            clinic_id=graph.clinic_a,
            title="Horário",
            body="Atendemos das 8h às 18h.",
            purpose="transactional",
        )
    old = timezone.now() - timedelta(hours=25)
    with owner_context(graph.organization_a):
        Conversation.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            provider_account_id=world.account,
            channel="whatsapp",
            purpose="transactional",
            contact_fingerprint=uuid4().hex + uuid4().hex,
            contact_address="+5511999990888",
            last_inbound_at=old,
            last_activity_at=old,
        )
    _, thread = _thread(graph)
    client, _ = receptionist_client(graph)
    assert "data-quick-replies" not in _get(client, thread)
