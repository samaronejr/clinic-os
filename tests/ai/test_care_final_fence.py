"""Observe locks and content-free admissions at the actual Care formatter."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from threading import Barrier, Event
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import pytest
from apps.ai import care_views, pending_outputs
from apps.ai.contracts import GatewayAccessDeniedError
from apps.ai.models import CareProposal
from apps.consent.decision_fence import try_hold_decision
from apps.consent.services import record_ai_disclosure
from django.db import connection, connections, transaction
from django.http import StreamingHttpResponse
from django.shortcuts import render
from django.template.response import SimpleTemplateResponse
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from ai import care_support
from ai.chart_consent_support import accept_ai
from ai.test_care_views import _client
from ai.test_pending_render_commit import refuse_plan_visit
from auth.test_session_lock_order import _await_blocked
from core.test_navigation import _get, _post
from core.test_workspace_boundaries import _normalized_denial
from ehr.test_autosave import as_actor

if TYPE_CHECKING:
    from pathlib import Path
    from uuid import UUID

    from apps.ai.pending_outputs import VisiblePending
    from django.http import HttpRequest, HttpResponse
    from django.test import Client

    from ai.care_support import CareWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass
class Page:
    care: CareWorld
    keys: tuple[pending_outputs.PendingKey[CareProposal], ...]
    client: Client
    url: str
    form: dict[str, str]


@pytest.fixture
def page(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    transport: str,
) -> Page:
    care = care_support.care_world(rbac_graph, monkeypatch, synthetic_secret_backend)
    accept_ai(rbac_graph, care.plan.enrollment_id)
    bundle = care_support.request_and_collect(care)
    keys = tuple(
        pending_outputs.key(row)
        for row in bundle.items
        if row.audience == "administrative"
    )
    assert len(keys) == 2
    client = _client(care.manager, "clinic_manager")
    client.defaults["HTTP_HX_REQUEST"] = "true" if transport == "htmx" else "false"
    url = reverse("ai:care-coordination", args=(rbac_graph.clinic_a,))
    form = {"action": "open", "invocation_id": str(bundle.invocation_id)}
    assert _get(client, url).status_code == 200
    positive = _post(client, url, form)
    assert positive.status_code == 200
    assert all(str(k.pk).encode() in positive.content for k in keys)
    return Page(care, keys, client, url, form)


def _exclusive(clinic: UUID) -> bool:
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
            [f"ai-pending-decision:{clinic}"],
        )
        row = cur.fetchone()
        return row is not None and row[0] is True


def _separate_exclusive(clinic: UUID) -> bool:
    try:
        return _exclusive(clinic)
    finally:
        connections.close_all()


def _live_count(page: Page) -> int:
    # Keep the independent row-policy witness. The formatter's own fenced
    # connection can no longer issue a raw query as a test probe.
    try:
        with as_actor(page.care.graph, page.care.manager):
            return CareProposal.objects.filter(pk__in=[k.pk for k in page.keys]).count()
    finally:
        connections.close_all()


@pytest.mark.parametrize("transport", ["native"])
def test_queued_withdrawal_precedes_later_readers(
    page: Page, transport: str, superuser_database_url: str
) -> None:
    g = page.care.graph
    writer_pid: Future[int] = Future()

    def reader() -> bool:
        try:
            with as_actor(g, page.care.manager):
                return try_hold_decision(clinic_id=g.clinic_a, using="default")
        finally:
            connections.close_all()

    def withdraw() -> None:
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_backend_pid()")
                writer_pid.set_result(cur.fetchone()[0])
            refuse_plan_visit(g, page.care)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with as_actor(g, page.care.manager):
            assert try_hold_decision(clinic_id=g.clinic_a, using="default")
            assert pool.submit(reader).result(timeout=30), "readers can share"
            finished = pool.submit(withdraw)
            pid = writer_pid.result(timeout=30)
            _await_blocked(superuser_database_url, pid)
            # Prove this backend's held shared advisory lock, not an unrelated
            # row lock, is the reason the actual withdrawal is in the queue.
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT FROM pg_locks w JOIN pg_locks r "
                    "USING (locktype, database, classid, objid, objsubid) "
                    "WHERE w.locktype='advisory' AND w.pid=%s AND NOT w.granted "
                    "AND w.mode='ExclusiveLock' AND r.pid=pg_backend_pid() "
                    "AND r.mode='ShareLock' AND r.granted)",
                    [pid],
                )
                assert cur.fetchone() == (True,)
            assert not pool.submit(reader).result(timeout=30), "reader overtook writer"
        finished.result(timeout=30)
        assert pool.submit(reader).result(timeout=30), "writer committed and released"
    with as_actor(g, page.care.manager):
        assert not CareProposal.objects.filter(
            pk__in=[k.pk for k in page.keys]
        ).exists()


def _refuse_at_formatter(
    care: CareWorld, entered: Event, attempt: Future[tuple[int, bool]]
) -> None:
    try:
        assert entered.wait(30)
        with connection.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            row = cur.fetchone()
            assert row is not None
            pid = int(row[0])
        attempt.set_result((pid, _exclusive(care.graph.clinic_a)))
        refuse_plan_visit(care.graph, care)
    finally:
        connections.close_all()


@pytest.mark.parametrize("transport", ["native", "htmx"])
def test_care_final_formatter_serializes_real_refusal(
    page: Page, transport: str, superuser_database_url: str
) -> None:
    entered = Event()
    attempt: Future[tuple[int, bool]] = Future()

    observations: list[tuple[int, int]] = []
    serialized: list[bool] = []
    fmt = care_views._rows
    with ThreadPoolExecutor(max_workers=1) as pool:
        completed = pool.submit(_refuse_at_formatter, page.care, entered, attempt)

        def at_formatter(
            clinic_id: UUID, items: tuple[VisiblePending[CareProposal], ...]
        ) -> tuple[care_views.ItemRow, ...]:
            assert connection.in_atomic_block
            entered.set()
            pid, unlocked = attempt.result(timeout=30)
            if unlocked:
                completed.result(timeout=30)
            else:
                _await_blocked(superuser_database_url, pid)
                with connection.cursor() as cur:
                    cur.execute(
                        "SELECT pg_backend_pid()=ANY(pg_blocking_pids(%s))", [pid]
                    )
                    assert cur.fetchone() == (True,)

            with ThreadPoolExecutor(max_workers=1) as observer:
                fresh = observer.submit(_live_count, page).result(timeout=30)
            if not fresh:
                # Observe admission only. Do not read a withdrawn field.
                observations.append(
                    (fresh, sum(h.project(lambda _row: True) is True for h in items))
                )
                return ()
            observations.append((fresh, len(items)))
            return fmt(clinic_id, items)

        def serialize(
            request: HttpRequest,
            template: str,
            context: dict[str, object],
            *,
            status: int,
        ) -> HttpResponse:
            response = render(request, template, context, status=status)
            serialized.append(bool(response.content) and not completed.done())
            return response

        try:
            with (
                patch.object(care_views, "_rows", at_formatter),
                patch.object(care_views, "render", serialize),
                CaptureQueriesContext(connection) as queries,
            ):
                response = _post(page.client, page.url, page.form)
        finally:
            entered.set()
        completed.result(timeout=30)
    assert observations == [(2, 2)], (
        "a source refusal committed before the final formatter"
    )
    assert serialized == [True], (
        "consent writer committed before response serialization"
    )
    assert response.status_code == 200
    assert all(str(k.pk).encode() in response.content for k in page.keys)
    sqls = [q["sql"] for q in queries.captured_queries]
    locks = [i for i, s in enumerate(sqls) if "pg_try_advisory_xact_lock_shared" in s]
    batch = [i for i, s in enumerate(sqls) if '"pending_render_group"' in s]
    assert len(locks) == 1
    assert len(batch) == 1
    assert locks[0] < batch[0]
    after = _post(page.client, page.url, page.form)
    assert after.status_code == 403
    assert all(str(k.pk).encode() not in after.content for k in page.keys)


@pytest.mark.parametrize("transport", ["native", "htmx"])
def test_uncommitted_choice_refuses_care_like_unknown(
    page: Page, transport: str
) -> None:
    g = page.care.graph
    unknown = _post(
        page.client, page.url, {"action": "open", "invocation_id": str(uuid4())}
    )
    assert unknown.status_code == 403
    synced = Barrier(2, timeout=30)
    release = Event()

    def writer() -> None:
        try:
            # Bind the physician before opening the choice; keep that real
            # tenant transaction open until the other request has returned.
            with as_actor(g, g.physician):
                record_ai_disclosure(
                    clinic_id=g.clinic_a,
                    encounter_id=page.care.plan.encounter_id,
                    informed=True,
                    refused=True,
                )
                synced.wait()
                assert release.wait(30)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        done = pool.submit(writer)
        try:
            synced.wait()
            # Refuse to materialize any fields if the parent admits a callback.
            with patch.object(care_views, "_rows", return_value=()) as fmt:
                response = _post(page.client, page.url, page.form)
        finally:
            release.set()
        done.result(timeout=30)
    assert response.status_code == unknown.status_code
    assert _normalized_denial(response) == _normalized_denial(unknown)
    assert not fmt.called
    assert _post(page.client, page.url, page.form).status_code == unknown.status_code


@pytest.mark.parametrize("transport", ["native"])
def test_retained_callback_is_an_independent_locked_read(
    page: Page, transport: str
) -> None:
    g = page.care.graph
    selector = page.keys[0]
    with as_actor(g, page.care.manager):
        handle = pending_outputs.ai_pending_visible(
            (selector,), lambda batch: batch.get(selector), clinic_id=g.clinic_a
        )
    assert handle is not None
    admitted = Event()
    probed: Future[bool] = Future()

    def choice() -> None:
        try:
            assert admitted.wait(30)
            probed.set_result(_exclusive(g.clinic_a))
            refuse_plan_visit(g, page.care)
        finally:
            connections.close_all()

    def callback(_fresh: VisiblePending[CareProposal]) -> bool:
        admitted.set()
        return not probed.result(timeout=30)

    with ThreadPoolExecutor(max_workers=1) as pool:
        finished = pool.submit(choice)
        try:
            with as_actor(g, page.care.manager):
                protected = handle.project(callback)
        finally:
            admitted.set()
        finished.result(timeout=30)
    ran = []
    with as_actor(g, page.care.manager):
        assert handle.project(lambda _fresh: ran.append(True)) is None
        unknown = pending_outputs.PendingKey(CareProposal, uuid4())
        assert (
            pending_outputs.ai_pending_visible(
                (unknown,), lambda b: b.get(unknown), clinic_id=g.clinic_a
            )
            is None
        )
    assert not ran
    assert protected is True, (
        "retained callback admitted without the current choice fence"
    )


@pytest.mark.parametrize("transport", ["native"])
def test_even_active_callback_rechecks_without_relocking(
    page: Page, transport: str
) -> None:
    g = page.care.graph
    selector = page.keys[0]

    def project(batch: pending_outputs.RenderSelection) -> bool:
        h = batch.get(selector)
        assert h is not None
        return h.project(lambda fresh: fresh is not h) is True

    with as_actor(g, page.care.manager), CaptureQueriesContext(connection) as q:
        independent = pending_outputs.ai_pending_visible(
            (selector,), project, clinic_id=g.clinic_a
        )
    assert independent, "a callback reused the earlier handle instead of revalidating"
    sqls = [s["sql"] for s in q.captured_queries]
    assert sum("pg_try_advisory_xact_lock_shared" in s for s in sqls) == 1
    assert sum('"pending_render_group"' in s for s in sqls) == 2


@pytest.mark.parametrize("transport", ["native"])
def test_reusing_the_captured_context_cannot_reopen_a_finished_frame(
    page: Page, transport: str
) -> None:
    g = page.care.graph
    selector = page.keys[0]
    with as_actor(g, page.care.manager):
        ctx, held = pending_outputs.ai_pending_visible(
            (selector,),
            lambda batch: (copy_context(), batch.get(selector)),
            clinic_id=g.clinic_a,
        )
    assert held is not None
    refuse_plan_visit(g, page.care)
    with as_actor(g, page.care.manager):
        assert not CareProposal.objects.filter(pk=selector.pk).exists()
        with pytest.raises(GatewayAccessDeniedError):
            ctx.run(lambda: held.record is not None)
        assert ctx.run(held.project, lambda _row: True) is None


@pytest.mark.parametrize("transport", ["native"])
def test_pending_boundary_materializes_lazy_bytes_and_refuses_streaming(
    page: Page, transport: str
) -> None:
    g = page.care.graph
    observed = []

    class Lazy(SimpleTemplateResponse):
        @property
        def rendered_content(self) -> str:
            observed.append(connection.in_atomic_block)
            return "synthetic"

    with as_actor(g, page.care.manager):
        response = pending_outputs.ai_pending_visible(
            page.keys, lambda _b: Lazy("unused"), clinic_id=g.clinic_a
        )
        assert response.is_rendered, "lazy response escaped its projection"
        assert response.content == b"synthetic"
        with pytest.raises(GatewayAccessDeniedError):
            pending_outputs.ai_pending_visible(
                page.keys, lambda _b: StreamingHttpResponse(()), clinic_id=g.clinic_a
            )
    assert observed == [True]


@pytest.mark.parametrize("transport", ["native"])
def test_rolled_back_render_lock_is_reacquired(page: Page, transport: str) -> None:
    g = page.care.graph
    with as_actor(g, page.care.manager), ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            assert try_hold_decision(clinic_id=g.clinic_a, using="default")
            transaction.set_rollback(True)
        assert pool.submit(_separate_exclusive, g.clinic_a).result(timeout=30)

        def project(batch: pending_outputs.RenderSelection) -> bool:
            assert len(batch.many(page.keys)) == 2
            return pool.submit(_separate_exclusive, g.clinic_a).result(timeout=30)

        assert not pending_outputs.ai_pending_visible(
            page.keys, project, clinic_id=g.clinic_a
        ), "savepoint rollback removed the lock before materialization"


@pytest.mark.parametrize("transport", ["native"])
def test_org_wide_read_refuses_while_a_source_clinic_choice_is_in_progress(
    page: Page, transport: str
) -> None:
    g = page.care.graph
    admitted, release = Event(), Event()

    def writer() -> None:
        try:
            with as_actor(g, g.physician):
                record_ai_disclosure(
                    clinic_id=g.clinic_a,
                    encounter_id=page.care.plan.encounter_id,
                    informed=True,
                    refused=True,
                )
                admitted.set()
                assert release.wait(30)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        done = pool.submit(writer)
        try:
            assert admitted.wait(30)
            with as_actor(g, page.care.manager):
                count = pending_outputs.ai_pending_visible(
                    page.keys, lambda b: len(b.many(page.keys))
                )
        finally:
            release.set()
        done.result(timeout=30)
    assert count == 0, "the org-wide read admitted work from a changing clinic"
