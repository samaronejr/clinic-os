"""Front-desk SQL guards on real roles: raw forgery, bridges and actor rechecks.

Raw ORM writes and direct bridge calls run with only database GUCs set, so no
Python service decides first: the grants, row policies and the
``ai_front_desk_guard`` / message guard triggers must refuse on their own.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.front_desk_kb import approve_article, draft_article
from apps.ai.models import FrontDeskThread, FrontDeskTurn, KnowledgeArticle
from apps.comms.inbox_inbound import inbound_context
from apps.comms.models import Message
from apps.core.secrets import secret_store
from apps.identity.models import User
from apps.tenancy.envelope import KEK_SECRET_NAME, protect
from django.db import DatabaseError, connection
from django.utils import timezone

from ai.test_front_desk import desk_world, say
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from ai.test_front_desk import Desk
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _forged_article(graph: RbacGraph, actor: UUID, **terms: object) -> KnowledgeArticle:
    values: dict[str, object] = {
        "organization_id": graph.organization_a,
        "clinic_id": graph.clinic_a,
        "key": "horario",
        "version": 9,
        "topic": "hours",
        "title": "Horário",
        "body": "Atendemos das 8h às 18h.",
        "keywords": ["horario"],
        "created_by_id": actor,
    }
    values.update(terms)
    return KnowledgeArticle(**values)


def _raw(sql: str, record: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(sql, [record])


def _refused(graph: RbacGraph, actor: UUID, forge: Callable[[], object]) -> None:
    with pytest.raises(DatabaseError), permission_context(graph, actor):
        forge()


def _answered(desk: Desk) -> tuple[FrontDeskThread, FrontDeskTurn]:
    assert say(desk, "Oi") is not None
    with owner_context(desk.graph.organization_a):
        return FrontDeskThread.objects.get(), FrontDeskTurn.objects.get()


def test_database_guards_refuse_raw_front_desk_forgery(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    thread, turn = _answered(desk)
    staff = desk.manager
    kek = secret_store().get_secret(KEK_SECRET_NAME)
    # A tenant envelope needs its tenant context; the bytes stay valid after it.
    with permission_context(rbac_graph, staff):
        body = protect(purpose="comms.message.body", plaintext=b"Resposta forjada")
    # Staff never write agent state; closing a waiting handoff is their only
    # update, and this thread is not waiting.
    for forge in (
        lambda: FrontDeskTurn.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            thread_id=thread.pk,
            inbound_id=turn.inbound_id,
            policy_id=turn.policy_id,
        ),
        lambda: FrontDeskThread.objects.filter(pk=thread.pk).update(
            state="resolved", resolved_by_id=staff, revision=thread.revision + 1
        ),
        lambda: FrontDeskThread.objects.filter(pk=thread.pk).update(
            verified_until=timezone.now() + timedelta(hours=1)
        ),
        lambda: _raw(
            "DELETE FROM clinic_app.ai_frontdeskthread WHERE id=%s", thread.pk
        ),
        lambda: FrontDeskTurn.objects.filter(pk=turn.pk).update(tool="scheduling.hold"),
    ):
        _refused(rbac_graph, staff, forge)

    def forged_reply() -> None:
        reply = uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.ai_front_desk_reply', %s, true)", [str(reply)]
            )
            cursor.execute(
                "INSERT INTO clinic_app.comms_message (id,organization_id,"
                "conversation_id,direction,body,sent_by_id,command_id,supported,"
                "provider_state,created_at) VALUES (%s,%s,%s,'out',%s,NULL,%s,true,"
                "'sent',now())",
                [
                    reply,
                    rbac_graph.organization_a,
                    thread.conversation_id,
                    body,
                    turn.pk,
                ],
            )

    # A sender-less reply exists only inside the finish bridge of a claimed turn.
    _refused(rbac_graph, staff, forged_reply)
    for call, arguments in (
        ("ai_front_desk_finish(%s,%s,%s,'greeting','','answered',NULL)", [body, kek]),
        ("ai_front_desk_state(%s,'offer',NULL,NULL,NULL,'')", []),
        ("ai_front_desk_verify(%s,'2000-01-02',%s)", [kek]),
    ):
        with (
            pytest.raises(DatabaseError),
            inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
            connection.cursor() as cursor,
        ):
            cursor.execute(f"SELECT clinic_app.{call}", [turn.pk, *arguments])
    with (
        pytest.raises(DatabaseError),
        inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
    ):
        forged_reply()
    # Outside the provider principal the bridges see no turn at all.
    with permission_context(rbac_graph, staff), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM clinic_app.ai_front_desk_pending()")
        assert cursor.fetchone() == (0,)
    # Reviewed answers: a draft by its author only, approval by the approver.
    for forged in (
        _forged_article(rbac_graph, staff, state="approved", approved_by_id=staff),
        _forged_article(rbac_graph, rbac_graph.shared_user),
    ):
        _refused(rbac_graph, staff, partial(forged.save, force_insert=True))
    with as_actor(rbac_graph, staff):
        draft = draft_article(
            clinic_id=rbac_graph.clinic_a,
            key="horario",
            topic="hours",
            title="Horário",
            body="Atendemos das 8h às 18h.",
            keywords=("horario",),
        )
    for forge in (
        lambda: KnowledgeArticle.objects.filter(pk=draft.pk).update(
            state="approved",
            approved_by_id=rbac_graph.shared_user,
            approved_at=timezone.now(),
            revision=2,
        ),
        lambda: KnowledgeArticle.objects.filter(pk=draft.pk).update(
            body="Atendemos 24 horas."
        ),
        lambda: _raw(
            "DELETE FROM clinic_app.ai_knowledgearticle WHERE id=%s", draft.pk
        ),
    ):
        _refused(rbac_graph, staff, forge)
    with owner_context(rbac_graph.organization_a):
        assert Message.objects.filter(direction="out").count() == 1
        assert KnowledgeArticle.objects.get().state == "draft"
        assert FrontDeskThread.objects.get().state == "active"


def test_front_desk_sql_rechecks_inactive_and_foreign_actor(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    assert say(desk, "Quero falar com um atendente") is not None
    with owner_context(rbac_graph.organization_a):
        thread = FrontDeskThread.objects.get()
    staff = desk.manager
    with as_actor(rbac_graph, staff):
        draft = draft_article(
            clinic_id=rbac_graph.clinic_a,
            key="horario",
            topic="hours",
            title="Horário",
            body="Atendemos das 8h às 18h.",
            keywords=("horario",),
        )
    # The graph's clinic_admin administers clinic B only: a foreign actor.
    foreign = rbac_graph.clinic_admin
    with owner_context(rbac_graph.organization_a):
        User.objects.filter(pk=staff).update(is_active=False)
    try:
        for actor in (staff, foreign):
            _refused(
                rbac_graph,
                actor,
                partial(_forged_article(rbac_graph, actor).save, force_insert=True),
            )
            with permission_context(rbac_graph, actor):
                approved = KnowledgeArticle.objects.filter(pk=draft.pk).update(
                    state="approved",
                    approved_by_id=actor,
                    approved_at=timezone.now(),
                    revision=draft.revision + 1,
                )
                resolved = FrontDeskThread.objects.filter(pk=thread.pk).update(
                    state="resolved",
                    resolved_by_id=actor,
                    revision=thread.revision + 1,
                )
            assert (approved, resolved) == (0, 0)
    finally:
        with owner_context(rbac_graph.organization_a):
            User.objects.filter(pk=staff).update(is_active=True)
    with owner_context(rbac_graph.organization_a):
        assert KnowledgeArticle.objects.get().state == "draft"
        assert FrontDeskThread.objects.get().state == "handoff_pending"
    # The reactivated administrator decides again through the same guard.
    with as_actor(rbac_graph, staff):
        assert (
            approve_article(
                clinic_id=rbac_graph.clinic_a,
                article_id=draft.pk,
                expected_revision=draft.revision,
            ).state
            == "approved"
        )
