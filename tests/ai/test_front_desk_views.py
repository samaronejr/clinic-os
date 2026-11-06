"""Todo 54 staff pages over HTTP on real roles: reviewed answers and handoffs.

Selectors travel in POST bodies; roles without the page's permission and
unknown selectors get the shared 403; a stale revision is a 409 that changes
nothing; the handoff list shows the reason and the time, never message text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai import front_desk
from apps.ai.front_desk_views import ARTICLE_INPUT, CONFLICT
from apps.ai.models import FrontDeskThread, KnowledgeArticle
from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext

from ai.test_front_desk import desk_world, pt, say
from core.test_navigation import _client_for, _get
from identity.permission_support import owner_context
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
OK = 200
BAD_REQUEST = 400
FORBIDDEN = 403
CONFLICTED = 409
ANSWER = {
    "action": "draft",
    "key": "horario",
    "topic": "hours",
    "title": "Horário de atendimento",
    "body": "Atendemos de segunda a sexta, das 8h às 18h.",
    "keywords": "horario, funcionamento, abre",
}


def _url(graph: RbacGraph, name: str) -> str:
    return reverse(f"ai:{name}", args=(graph.clinic_a,))


def _post(
    client: Client, path: str, data: dict[str, str]
) -> _MonkeyPatchedWSGIResponse:
    form = {"csrfmiddlewaretoken": client.cookies[settings.CSRF_COOKIE_NAME].value}
    form.update(data)
    with runtime_role(), fixed_otp_time():
        return client.post(path, form)


def _html(response: _MonkeyPatchedWSGIResponse, status: int = OK) -> str:
    assert response.status_code == status
    return response.content.decode()


def _versions(graph: RbacGraph) -> list[tuple[int, str, int]]:
    with owner_context(graph.organization_a):
        return list(
            KnowledgeArticle.objects.order_by("version").values_list(
                "version", "state", "revision"
            )
        )


def test_knowledge_page_drafts_approves_and_retires_exact_versions(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    admin, _user = _client_for(graph, "clinic_admin")
    url = _url(graph, "front-desk-knowledge")
    page = _get(admin, url)
    assert {"private", "no-store"} <= {
        part.strip() for part in page["Cache-Control"].split(",")
    }
    assert gettext(
        "No answers yet: the assistant forwards every question to the team."
    ) in _html(page)
    for refused in (
        {**ANSWER, "key": "Horário!"},
        {**ANSWER, "topic": "diagnosis"},
        {**ANSWER, "body": "Ligue 5555-0000 ou escreva para equipe@clinica.invalid."},
        {**ANSWER, "keywords": ""},
        {**ANSWER, "link": "https://clinica.invalid/preparo?paciente=1"},
    ):
        assert gettext(ARTICLE_INPUT) in _html(_post(admin, url, refused), BAD_REQUEST)
    assert _versions(graph) == []
    saved = _html(_post(admin, url, ANSWER))
    assert gettext("Draft saved. Approve it to let the assistant answer with it.") in (
        saved
    )
    with owner_context(graph.organization_a):
        first = KnowledgeArticle.objects.get()
    approve = {"action": "approve", "article_id": str(first.pk)}
    assert gettext(CONFLICT) in _html(
        _post(admin, url, {**approve, "revision": "2"}), CONFLICTED
    )
    unknown = {"action": "approve", "article_id": str(uuid4()), "revision": "1"}
    assert _post(admin, url, unknown).status_code == FORBIDDEN
    assert _post(admin, url, {"action": "approve"}).status_code == BAD_REQUEST
    approved = _html(_post(admin, url, {**approve, "revision": "1"}))
    assert gettext("Answer approved. The assistant now uses this version.") in approved
    # The next version replaces the approved one only once it is approved.
    _post(
        admin, url, {**ANSWER, "body": "Atendemos de segunda a sábado, das 8h às 18h."}
    )
    assert _versions(graph) == [(1, "approved", 2), (2, "draft", 1)]
    with owner_context(graph.organization_a):
        second = KnowledgeArticle.objects.get(version=2)
    _post(
        admin, url, {"action": "approve", "article_id": str(second.pk), "revision": "1"}
    )
    assert _versions(graph) == [(1, "retired", 3), (2, "approved", 2)]
    retired = _html(
        _post(
            admin,
            url,
            {"action": "retire", "article_id": str(second.pk), "revision": "2"},
        )
    )
    assert gettext("Answer retired. The assistant no longer uses it.") in retired
    assert _versions(graph) == [(1, "retired", 3), (2, "retired", 3)]
    receptionist, _user = _client_for(graph, "receptionist")
    assert _get(receptionist, url).status_code == FORBIDDEN
    assert _post(receptionist, url, ANSWER).status_code == FORBIDDEN
    assert len(_versions(graph)) == 2


def test_handoff_page_lists_reasons_and_resolves_by_post_body(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    text = "Quero falar com um atendente, por favor"
    assert say(desk, text) == f"{pt(front_desk.DISCLOSURE)}\n{pt(front_desk.HUMAN)}"
    with owner_context(rbac_graph.organization_a):
        thread = FrontDeskThread.objects.get()
    receptionist, _user = _client_for(rbac_graph, "receptionist")
    url = _url(rbac_graph, "front-desk-handoffs")
    html = _html(_get(receptionist, url))
    assert gettext("Asked for a person") in html
    assert str(thread.pk) in html
    assert text not in html
    assert _post(
        receptionist, url, {"thread_id": "x", "revision": "1"}
    ).status_code == (BAD_REQUEST)
    unknown = {"thread_id": str(uuid4()), "revision": str(thread.revision)}
    assert _post(receptionist, url, unknown).status_code == FORBIDDEN
    stale = {"thread_id": str(thread.pk), "revision": str(thread.revision + 1)}
    assert gettext(CONFLICT) in _html(_post(receptionist, url, stale), CONFLICTED)
    finance, _user = _client_for(rbac_graph, "finance")
    current = {"thread_id": str(thread.pk), "revision": str(thread.revision)}
    assert _get(finance, url).status_code == FORBIDDEN
    assert _post(finance, url, current).status_code == FORBIDDEN
    resolved = _html(_post(receptionist, url, current))
    assert (
        gettext("Marked as resolved. The assistant answers new messages again.")
        in resolved
    )
    assert gettext("No conversation is waiting for a person.") in resolved
    with owner_context(rbac_graph.organization_a):
        thread = FrontDeskThread.objects.get()
    assert (thread.state, thread.handoff_reason) == ("resolved", "human_request")
