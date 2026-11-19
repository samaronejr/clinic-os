"""Body-selected inbox threads retain native POST, CSRF and denial boundaries."""

from __future__ import annotations

import html as html_module
import re
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import pytest
from apps.comms.inbox_views import ACTIONS, REOPEN_SESSION_KEY
from apps.comms.models import Conversation, InternalNote, Message
from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext

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


def _assert_session_untouched(response: _MonkeyPatchedWSGIResponse, name: str) -> None:
    """A refusal neither modifies the session nor sets the session cookie."""
    assert response.wsgi_request.session.modified is False, name
    assert settings.SESSION_COOKIE_NAME not in response.cookies, name


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
    selector = str(conversation.pk)
    assert all(selector not in morsel.value for morsel in saved.cookies.values())
    # Only the server-side session holds the return target, once.
    assert client.session[REOPEN_SESSION_KEY] == selector
    with runtime_role(), fixed_otp_time():
        refresh = client.get(
            path, headers={"HX-Request": "true", "HX-Target": "conversation-list"}
        )
    assert refresh.status_code == 200
    assert client.session[REOPEN_SESSION_KEY] == selector
    reopened = _get(client, path)
    assert reopened.status_code == 200
    assert "thread-region" in Document(reopened.content).identifiers()
    assert f'name="conversation_id" value="{selector}"' in reopened.content.decode()
    assert REOPEN_SESSION_KEY not in client.session
    listed = _get(client, path)
    assert listed.status_code == 200
    identifiers = Document(listed.content).identifiers()
    assert "thread-region" not in identifiers
    assert "conversation-list" in identifiers
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


def test_a_refused_action_also_returns_to_its_thread(
    rbac_graph: RbacGraph, thread: tuple[Client, Conversation]
) -> None:
    # The refusal is read where the user acted, as before the POST selection.
    client, conversation = thread
    path = reverse("comms:inbox", args=(rbac_graph.clinic_a,))
    refused = _post(
        client,
        path,
        {
            "conversation_id": str(conversation.pk),
            "action": "assign",
            "assignee": "me",
            "expected_revision": "999",
        },
    )
    assert refused.status_code == 302
    assert refused["Location"] == path
    page = _get(client, path)
    assert "thread-region" in Document(page.content).identifiers()
    assert gettext("The conversation changed. Refresh and try again.") in (
        page.content.decode()
    )
    with owner_context(rbac_graph.organization_a):
        assert Conversation.objects.get().assignee_id is None


def test_a_stored_unknown_or_malformed_reopen_shows_the_list(
    rbac_graph: RbacGraph, thread: tuple[Client, Conversation]
) -> None:
    # Consuming a bad return target is housekeeping, not a refusal: the GET is
    # the same list as with no key (no 404, no lockout), and the key is gone.
    client, conversation = thread
    path = reverse("comms:inbox", args=(rbac_graph.clinic_a,))
    plain = Document(_get(client, path).content).identifiers()
    assert "conversation-list" in plain
    assert "thread-region" not in plain
    unknown = str(uuid4())
    for stored in (unknown, "malformed"):
        session = client.session
        session[REOPEN_SESSION_KEY] = stored
        session.save()
        listed = _get(client, path)
        assert listed.status_code == 200
        assert Document(listed.content).identifiers() == plain
        assert unknown not in listed.content.decode()
        assert REOPEN_SESSION_KEY not in client.session
    assert f'name="conversation_id" value="{conversation.pk}"' in (
        _get(client, path).content.decode()
    )


def test_every_inbox_refusal_exit_is_the_one_denial(
    rbac_graph: RbacGraph, thread: tuple[Client, Conversation]
) -> None:
    # Each refusal exit in apps/comms/inbox_views.py, driven through the real
    # view: the same 404 body as that actor's baseline denial, no selector in
    # it, the session untouched (not modified, no session cookie) and no
    # write. (The refusal-exit census requires each one reached.)
    client, conversation = thread
    path = reverse("comms:inbox", args=(rbac_graph.clinic_a,))
    selector = str(conversation.pk)

    with owner_context(rbac_graph.organization_a):
        before = (
            writes(),
            InternalNote.objects.count(),
            Message.objects.count(),
            Conversation.objects.count(),
        )
    # _conversation_post: an unknown body-selected thread (the baseline).
    baseline = _post(client, path, {"action": "show", "conversation_id": str(uuid4())})
    assert baseline.status_code == 404
    denials = {
        # inbox: a thread action without a conversation_id.
        "missing selector": _post(client, path, {"action": "assign"}),
        # inbox: opening a conversation for an unknown enrollment.
        "unknown enrollment": _post(
            client,
            path,
            {
                "action": "open",
                "enrollment_id": str(uuid4()),
                "channel": "whatsapp",
                "purpose": "transactional",
            },
        ),
    }
    for name, response in {"unknown thread": baseline, **denials}.items():
        assert response.status_code == 404, name
        assert response.content == baseline.content, name
        assert selector not in response.content.decode(), name
        _assert_session_untouched(response, name)
    # inbox: the list itself, for a role the inbox refuses (list_conversations).
    allied, _user = _client_for(rbac_graph, "allied_professional")
    allied_baseline = _post(
        allied, path, {"action": "show", "conversation_id": str(uuid4())}
    )
    refused_list = _get(allied, path)
    assert allied_baseline.status_code == refused_list.status_code == 404
    assert refused_list.content == allied_baseline.content
    assert selector not in refused_list.content.decode()
    _assert_session_untouched(allied_baseline, "allied baseline")
    _assert_session_untouched(refused_list, "refused list")
    # A refused role holding a return target: the list refusal is decided
    # before the key is read, so the 404 is the same, the session is untouched
    # and the key stays (it can never be consumed by this role).
    session = allied.session
    session[REOPEN_SESSION_KEY] = selector
    session.save()
    for attempt in ("first", "second"):
        keyed = _get(allied, path)
        assert keyed.status_code == 404, attempt
        assert keyed.content == allied_baseline.content, attempt
        _assert_session_untouched(keyed, f"refused list with a key, {attempt}")
        assert allied.session[REOPEN_SESSION_KEY] == selector, attempt
    with owner_context(rbac_graph.organization_a):
        after = (
            writes(),
            InternalNote.objects.count(),
            Message.objects.count(),
            Conversation.objects.count(),
        )
    assert after == before
