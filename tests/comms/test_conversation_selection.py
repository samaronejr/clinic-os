"""Body-selected inbox threads retain native POST, CSRF and denial boundaries."""

from __future__ import annotations

import html as html_module
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import pytest
from apps.comms.inbox_views import ACTIONS
from apps.comms.models import Conversation, InternalNote, Message
from django.urls import reverse

from accessible_document import Document
from comms.support import inbox_account, manager_of
from core.test_navigation import _client_for, _get, _post
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role
from workflow_view_support import writes

if TYPE_CHECKING:
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SELECTOR_QUERIES = ("conversation_id", "conversation")
URL_ATTRIBUTES = re.compile(
    r'\s(?:href|action|src|hx-get|hx-post|hx-push-url|hx-replace-url)="([^"]*)"'
)


def _generated_urls(html: str) -> list[str]:
    """Every URL-valued attribute of the page, unescaped as a browser parses it."""
    return [html_module.unescape(url) for url in URL_ATTRIBUTES.findall(html)]


@pytest.fixture
def thread(
    rbac_graph: RbacGraph, settings: SettingsWrapper
) -> tuple[Client, Conversation]:
    graph = rbac_graph
    manager_of(graph)
    world = inbox_account(graph, settings)
    world.receive(world.message_body(text="Mensagem administrativa sintetica."))
    with owner_context(graph.organization_a):
        conversation = Conversation.objects.get()
    client, _user = _client_for(graph, "receptionist")
    return client, conversation


def _assert_selected(
    response: _MonkeyPatchedWSGIResponse, path: str, conversation: Conversation
) -> None:
    assert response.status_code == 200
    document = Document(response.content)
    refresh = document.attributes_for("thread-region")
    assert refresh["hx-post"] == path
    assert refresh["hx-include"] == "#thread-selection"
    assert "hx-get" not in refresh
    assert document.attributes_for("thread-selection")["method"] == "post"
    selectors = [
        field
        for field in document.tagged("input")
        if field.get("name") == "conversation_id"
    ]
    assert len(selectors) >= 5
    assert all(
        field["type"] == "hidden" and field["value"] == str(conversation.pk)
        for field in selectors
    )
    for _tag, attributes in document.elements:
        for name in ("href", "action", "hx-get", "hx-post"):
            assert str(conversation.pk) not in (attributes.get(name) or "")
    assert {"private", "no-store"} <= {
        part.strip() for part in response["Cache-Control"].split(",")
    }


def test_selection_and_read_actions_keep_the_selector_in_post(
    rbac_graph: RbacGraph, thread: tuple[Client, Conversation]
) -> None:
    client, conversation = thread
    path = reverse("comms:inbox", args=(rbac_graph.clinic_a,))
    selector = {"conversation_id": str(conversation.pk)}
    for action in ("show", "find_patient", "quick_reply"):
        response = _post(
            client, path, {**selector, "action": action, "q": "Sintetico Ausente"}
        )
        _assert_selected(response, path, conversation)
    # A GET never uses a record selector to open a thread.
    listing = _get(client, f"{path}?conversation_id={conversation.pk}")
    assert listing.status_code == 200
    assert "thread-region" not in Document(listing.content).identifiers()
    # Nor does an ignored selector-like query reach any generated URL. The
    # selector is not a listed row, so it may appear nowhere at all; the
    # listed row keeps its id only in the POST opener's hidden field (SC-7).
    ignored = str(uuid4())
    hostile_states = (
        ignored,
        f"open&conversation_id={ignored}",
        f"pending&conversation={ignored}",
    )
    requests = [
        *(f"{path}?state=open&{query}={ignored}" for query in SELECTOR_QUERIES),
        *(f"{path}?{urlencode({'state': state})}" for state in hostile_states),
    ]
    for request_path in requests:
        echoed = _get(client, request_path)
        html = echoed.content.decode()
        assert echoed.status_code == 200
        assert ignored not in html
        urls = _generated_urls(html)
        assert urls
        for url in urls:
            assert str(conversation.pk) not in url
            assert not set(parse_qs(urlsplit(url).query)) & set(SELECTOR_QUERIES)
        refresh = Document(echoed.content).attributes_for("conversation-list")
        assert refresh["hx-get"] == f"{path}?state=open"
        assert f'name="conversation_id" value="{conversation.pk}"' in html


def test_selected_mutation_redirects_without_a_selector_or_repeat(
    rbac_graph: RbacGraph, thread: tuple[Client, Conversation]
) -> None:
    graph = rbac_graph
    client, conversation = thread
    path = reverse("comms:inbox", args=(graph.clinic_a,))
    saved = _post(
        client,
        path,
        {
            "conversation_id": str(conversation.pk),
            "action": "note",
            "body": "Nota administrativa sintetica.",
        },
    )
    assert saved.status_code == 302
    assert saved["Location"] == path
    assert _get(client, path).status_code == 200
    assert _get(client, path).status_code == 200
    with owner_context(graph.organization_a):
        assert InternalNote.objects.filter(conversation=conversation).count() == 1


def test_missing_malformed_unknown_and_csrf_denials_do_not_write(
    rbac_graph: RbacGraph, thread: tuple[Client, Conversation]
) -> None:
    graph = rbac_graph
    client, conversation = thread
    path = reverse("comms:inbox", args=(graph.clinic_a,))
    session = dict(client.session)
    with owner_context(graph.organization_a):
        before = writes(), InternalNote.objects.count(), Message.objects.count()
    baseline = _post(client, path, {"action": "show"})
    assert baseline.status_code == 404
    for action in ("show", "quick_reply", *ACTIONS):
        missing = _post(client, path, {"action": action})
        assert missing.status_code == baseline.status_code
        assert missing.content == baseline.content
        assert not missing.cookies
        assert missing.wsgi_request.session.modified is False
    for action in ("show", "find_patient", "note"):
        for selector in ("", "malformed", str(uuid4())):
            response = _post(
                client,
                path,
                {
                    "action": action,
                    "conversation_id": selector,
                    "body": "Nao deve ser registrada.",
                    "q": "Sintetico Ausente",
                },
            )
            assert response.status_code == baseline.status_code
            assert response.content == baseline.content
            assert not response.cookies
            assert response.wsgi_request.session.modified is False
    with runtime_role(), fixed_otp_time():
        csrf = client.post(
            path, {"action": "show", "conversation_id": str(conversation.pk)}
        )
    assert csrf.status_code == 403
    assert dict(client.session) == session
    with owner_context(graph.organization_a):
        assert (writes(), InternalNote.objects.count(), Message.objects.count()) == (
            before
        )


def test_threads_open_by_post_and_no_url_carries_a_thread_id(
    thread: tuple[Client, Conversation],
) -> None:
    # SC-7: the old /messages/<id>/ GET route is gone, and the inbox list opens
    # each thread through a CSRF-protected POST carrying the id in its body.
    client, conversation = thread
    inbox = reverse("comms:inbox", args=(conversation.clinic_id,))
    assert _get(client, f"{inbox}{conversation.pk}/").status_code == 404
    page = _get(client, inbox)
    html = page.content.decode()
    assert page.status_code == 200
    assert f"/{conversation.pk}" not in html
    opener = html.index(f'name="conversation_id" value="{conversation.pk}"')
    form = html[html.rindex("<form", 0, opener) : opener]
    assert f'method="post" action="{inbox}"' in form
    assert 'name="csrfmiddlewaretoken"' in form
    assert 'name="action" value="show"' in form
    _assert_selected(
        _post(
            client, inbox, {"action": "show", "conversation_id": str(conversation.pk)}
        ),
        inbox,
        conversation,
    )
