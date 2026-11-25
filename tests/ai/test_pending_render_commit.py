"""Pending AI output at render time while another session changes consent.

Every ordering is forced with ``threading.Event`` or a waited future, never a
sleep. The patient's or clinician's change runs on its own database connection.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING, TypeVar, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from apps.ai import (
    agent_views,
    care_coordination,
    care_views,
    pending_outputs,
    revenue_views,
)
from apps.ai.agent_presenter import present_proposal
from apps.ai.approvals import reject_action
from apps.ai.contracts import GatewayAccessDeniedError
from apps.ai.models import ActionProposal, AIArtifact, CareProposal
from apps.consent.services import record_ai_disclosure
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from ai import care_support, revenue_support
from ai.agent_support import agent_world, propose
from ai.chart_consent_support import accept_ai
from ai.pending_display_support import withdraw
from ai.test_care_views import _client
from ai.test_revenue_cycle import finance_setup, proposal_for, world
from ai.test_revenue_views import post
from auth.test_session_lock_order import _await_blocked
from billing.journal_support import finance_scope
from core.test_navigation import _get, _post
from ehr.test_autosave import as_actor
from identity.lifecycle_test_support import verified_client
from otp_test_support import fixed_otp_time
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path
    from uuid import UUID

    from apps.ai.care_coordination import CareBundle
    from apps.ai.pending_outputs import VisiblePending
    from django.db.models import Model
    from django.test import Client
    from pytest_django.fixtures import SettingsWrapper

    from ai.care_support import CareWorld
    from ai.revenue_support import RevenueWorld
    from billing.journal_support import FinanceSetup
    from rbac_fixtures import RbacGraph

__all__ = ("finance_setup", "world")
pytestmark = pytest.mark.django_db(transaction=True)
T = TypeVar("T")
SURFACES = ("queue", "finance")
TRANSPORTS = ("native", "htmx")


@contextmanager
def commit_between_load_and_render(
    change: Callable[[], None],
) -> Iterator[Callable[[T], T]]:
    """Yield a fence; the change commits on another connection when it is hit."""
    loaded, committed = Event(), Event()

    def write_choice() -> None:
        try:
            assert loaded.wait(30), "authorized row was not loaded"
            change()
        finally:
            connections.close_all()
            committed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        completed = pool.submit(write_choice)

        def after_load(value: T) -> T:
            loaded.set()
            assert committed.wait(30), "choice did not commit before rendering"
            completed.result()
            return value

        try:
            yield after_load
        finally:
            loaded.set()
            completed.result()


@contextmanager
def final_batches() -> Iterator[list[bool]]:
    """Record only how many render batches ran and in which transaction state."""
    batches: list[bool] = []

    def observe(
        execute: Callable[..., object],
        sql: object,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        if '"pending_render_group"' in str(sql):
            batches.append(connection.in_atomic_block)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(observe):
        yield batches


@dataclass(frozen=True)
class ProposalPage:
    """One authorized reviewer's queue or Finance page for a real proposal."""

    client: Client
    url: str
    surface: str
    form: dict[str, str]
    markers: tuple[bytes, ...]
    proposal: UUID
    output: UUID
    patient: UUID
    receipt: UUID

    def fetch(self) -> tuple[int, bytes]:
        if self.surface == "queue":
            with runtime_role(), fixed_otp_time():
                result = self.client.get(self.url)
        else:
            result = post(self.client, self.url, self.form)
        return result.status_code, result.content


def proposal_page(world: RevenueWorld, surface: str, transport: str) -> ProposalPage:
    g = world.setup.graph
    if surface == "queue":
        # The repository's authorized Finance plus Automations queue world.
        revenue_support.add_clinic_manager_role(world.setup)
    patient, receipt = accept_ai(g, world.setup.appointment.enrollment_id)
    review = revenue_support.request_and_collect(world)
    assert review.output_id is not None
    with finance_scope(world.setup):
        original = proposal_for(world, review)
    item = next(item for item in review.items if item.kind == "appeal.draft")
    client = verified_client(g)
    client.defaults["HTTP_HX_REQUEST"] = "true" if transport == "htmx" else "false"
    name = "ai:agents" if surface == "queue" else "ai:revenue-cycle"
    url = reverse(name, args=(g.clinic_a,))
    with runtime_role(), fixed_otp_time():
        assert client.get(url).status_code == 200
    return ProposalPage(
        client,
        url,
        surface,
        {
            "command": "propose",
            "output_id": str(review.output_id),
            "position": str(item.position),
        },
        (str(original.pk).encode(), original.digest.encode()),
        original.pk,
        review.output_id,
        patient,
        receipt,
    )


@pytest.mark.parametrize("repeat", [1, 2])
def test_retained_finance_handle_cannot_reissue_a_reviewable_digest(
    world: RevenueWorld, repeat: int
) -> None:
    patient, receipt = accept_ai(
        world.setup.graph, world.setup.appointment.enrollment_id
    )
    review = revenue_support.request_and_collect(world)
    with finance_scope(world.setup):
        proposal = proposal_for(world, review)
    with finance_scope(world.setup):
        raw = ActionProposal.objects.select_related("clinic", "run__policy").get(
            pk=proposal.pk
        )
        expected = (raw.pk, raw.digest)
        # Same oracle across the incompatible acquisition APIs: keep exactly
        # what that revision allows its real public presenter to accept.
        boundary = getattr(pending_outputs, "ai_pending_visible", None)
        retained = (
            raw
            if boundary is None
            else boundary(
                (pending_outputs.key(raw),),
                lambda batch: batch.get(pending_outputs.key(raw)),
            )
        )
        handle = cast("VisiblePending[ActionProposal]", retained)
        positive = present_proposal(handle)
        assert positive is not None
        assert positive.reviewable
        assert (positive.proposal_id, positive.digest) == expected
    withdraw(patient, receipt)
    with finance_scope(world.setup):
        assert not ActionProposal.objects.filter(pk=raw.pk).exists()
        assert present_proposal(handle) is None


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("surface", SURFACES)
def test_withdrawal_committed_after_the_first_load_is_never_rendered(
    world: RevenueWorld, surface: str, transport: str
) -> None:
    page = proposal_page(world, surface, transport)
    status, before = page.fetch()
    assert status == 200
    assert all(marker in before for marker in page.markers)
    status, after = fetch_after_load(page, lambda: withdraw(page.patient, page.receipt))
    assert status in {200, 403}
    assert all(marker not in after for marker in page.markers)


def fetch_after_load(
    page: ProposalPage, change: Callable[[], None]
) -> tuple[int, bytes]:
    # Fence the first authorized row. No check, presenter or response is stubbed.
    surface = page.surface
    model: type[Model] = ActionProposal if surface == "queue" else AIArtifact
    target = page.proposal if surface == "queue" else page.output
    load = model.from_db
    reached: list[bool] = []
    with commit_between_load_and_render(change) as fence:

        def loaded(db: str, field_names: list[str], values: Sequence[object]) -> Model:
            row = load(db, field_names, values)
            if row.pk == target and not reached:
                reached.append(connection.in_atomic_block)
                fence(None)
            return row

        with patch.object(model, "from_db", side_effect=loaded):
            status, after = page.fetch()
    assert reached == [True]
    return status, after


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_queue_withdrawal_keeps_the_current_decision_of_a_visible_sibling(
    finance_setup: FinanceSetup,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    settings: SettingsWrapper,
    transport: str,
) -> None:
    # The other row is an operational checklist, independent of the patient's
    # consent. No action is executed. A real reviewer rejects it during load.
    g = finance_setup.graph
    admin = agent_world(g, monkeypatch, synthetic_secret_backend, autonomy="A1")
    sibling = propose(admin)
    settings.INSURANCE_SYNTHETIC_ENABLED = True
    revenue = revenue_support.revenue_world(
        finance_setup, monkeypatch, synthetic_secret_backend
    )
    page = proposal_page(revenue, "queue", transport)
    marker = f'data-agent-proposal="{sibling.pk}" data-agent-state="'.encode()
    status, before = page.fetch()
    assert status == 200
    assert marker + b'validated"' in before
    assert sibling.digest.encode() in before
    assert all(m in before for m in page.markers)

    def change() -> None:
        withdraw(page.patient, page.receipt)
        with as_actor(g, admin.gateway.administrator):
            reject_action(
                clinic_id=g.clinic_a,
                proposal_id=sibling.pk,
                expected_digest=sibling.digest,
            )

    status, after = fetch_after_load(page, change)
    assert status == 200
    assert all(m not in after for m in page.markers)
    assert marker + b'rejected"' in after
    assert sibling.digest.encode() not in after  # no longer an approval form


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("surface", SURFACES)
def test_authorized_proposal_page_runs_one_final_visibility_batch(
    world: RevenueWorld, surface: str, transport: str
) -> None:
    page = proposal_page(world, surface, transport)
    with final_batches() as batches:
        status, content = page.fetch()
    assert status == 200
    assert all(marker in content for marker in page.markers)
    assert batches == [True], "one batched check inside the response transaction"


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("decision", ["patient", "visit"])
def test_withdrawal_after_the_source_check_waits_for_the_response(
    world: RevenueWorld,
    surface: str,
    transport: str,
    decision: str,
    superuser_database_url: str,
) -> None:
    page = proposal_page(world, surface, transport)
    start = Event()
    writer: Future[int] = Future()

    def attempt() -> None:
        try:
            assert start.wait(30), "request did not reach the passed source check"
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                writer.set_result(cursor.fetchone()[0])
            if decision == "patient":
                withdraw(page.patient, page.receipt)
            else:
                refuse_guide_visit(world)
        finally:
            connections.close_all()

    def wait_until_blocked() -> None:
        start.set()
        pid = writer.result(timeout=30)
        _await_blocked(superuser_database_url, pid)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid() = ANY(pg_blocking_pids(%s))", [pid])
            assert cursor.fetchone() == (True,), (
                "writer must be waiting for this response"
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        finished = pool.submit(attempt)
        try:
            status, during = fetch_after_source_check(page, wait_until_blocked)
        finally:
            start.set()
        finished.result(timeout=30)
    # Database lock observation, not a timer, establishes render-before-commit.
    assert status == 200
    assert all(marker in during for marker in page.markers)
    status, after = page.fetch()
    assert status in {200, 403}
    assert all(marker not in after for marker in page.markers)


def fetch_after_source_check(
    page: ProposalPage, hook: Callable[[], None]
) -> tuple[int, bytes]:
    """Fetch the page; run ``hook`` in the request once its source check passed."""
    if page.surface == "queue":
        source_check = agent_views._visible

        def checked_queue(proposal: ActionProposal) -> bool:
            visible = source_check(proposal)
            if visible and proposal.pk == page.proposal:
                hook()
            return visible

        with patch.object(agent_views, "_visible", checked_queue):
            return page.fetch()
    source_check_review = revenue_views._review_for_proposal

    def checked_finance(clinic_id: UUID, proposal: ActionProposal) -> object:
        review = source_check_review(clinic_id, proposal)
        hook()
        return review

    with patch.object(revenue_views, "_review_for_proposal", checked_finance):
        return page.fetch()


def refuse_guide_visit(world: RevenueWorld) -> None:
    g = world.setup.graph
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a,
            encounter_id=world.cycle.guide.encounter_id,
            informed=True,
            refused=True,
        )


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("surface", SURFACES)
def test_visit_refusal_committed_after_the_load_is_never_rendered(
    world: RevenueWorld, surface: str, transport: str
) -> None:
    page = proposal_page(world, surface, transport)
    status, before = page.fetch()
    assert status == 200
    assert all(marker in before for marker in page.markers)
    # After the audit append the writer must wait (separate lock-graph proof).
    # Here the same writer really commits while the loaded page is in flight.
    status, after = fetch_after_load(page, lambda: refuse_guide_visit(world))
    assert status in {200, 403}
    assert all(marker not in after for marker in page.markers)


def refuse_plan_visit(g: RbacGraph, care: CareWorld) -> None:
    with as_actor(g, g.physician):
        record_ai_disclosure(
            clinic_id=g.clinic_a,
            encounter_id=care.plan.encounter_id,
            informed=True,
            refused=True,
        )


def test_care_preview_uses_the_batch_for_its_displayed_approval_digest(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
) -> None:
    care = care_support.care_world(rbac_graph, monkeypatch, synthetic_secret_backend)
    accept_ai(rbac_graph, care.plan.enrollment_id)
    bundle = care_support.request_and_collect(care)
    client = _client(care.manager, "clinic_manager")
    url = reverse("ai:care-coordination", args=(rbac_graph.clinic_a,))
    assert _get(client, url).status_code == 200
    with CaptureQueriesContext(connection) as captured:
        preview = _post(
            client,
            url,
            {"action": "preview", "invocation_id": str(bundle.invocation_id)},
        )
    assert preview.status_code == 200
    assert preview.content.count(b"data-care-preview-item=") == 2
    assert b'name="digest"' in preview.content
    queries = [query["sql"] for query in captured.captured_queries]
    frames = [i for i, sql in enumerate(queries) if '"pending_render_group"' in sql]
    assert len(frames) == 1
    assert not any('"ai_careproposal"' in sql for sql in queries[frames[0] + 1 :])


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_care_open_drops_items_refused_after_authorized_load(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
    transport: str,
) -> None:
    g = rbac_graph
    care = care_support.care_world(g, monkeypatch, synthetic_secret_backend)
    accept_ai(g, care.plan.enrollment_id)
    initial = care_support.request_and_collect(care)
    keys = [
        str(row.pk).encode()
        for row in initial.items
        if row.audience == "administrative"
    ]
    assert len(keys) == 2
    client = _client(care.manager, "clinic_manager")
    client.defaults["HTTP_HX_REQUEST"] = "true" if transport == "htmx" else "false"
    url = reverse("ai:care-coordination", args=(g.clinic_a,))
    form = {"action": "open", "invocation_id": str(initial.invocation_id)}
    assert _get(client, url).status_code == 200
    initial_page = _post(client, url, form)
    assert initial_page.status_code == 200
    assert all(k in initial_page.content for k in keys)

    lookup = care_coordination.care_bundle
    with commit_between_load_and_render(lambda: refuse_plan_visit(g, care)) as fence:

        def loaded(*, clinic_id: UUID, invocation_id: UUID) -> CareBundle:
            return fence(lookup(clinic_id=clinic_id, invocation_id=invocation_id))

        with patch.object(care_views, "care_bundle", loaded):
            after = _post(client, url, form)
    assert after.status_code in (200, 403)
    assert all(k not in after.content for k in keys)


def _record(handle: VisiblePending[CareProposal]) -> CareProposal:
    return handle.record


def test_one_batch_keeps_visible_siblings_and_rechecks_retained_handles(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_secret_backend: Path,
) -> None:
    g = rbac_graph
    care = care_support.care_world(g, monkeypatch, synthetic_secret_backend)
    accept_ai(g, care.plan.enrollment_id)
    bundle = care_support.request_and_collect(care)
    first, second = (row for row in bundle.items if row.audience == "administrative")
    keys = (
        pending_outputs.key(first),
        pending_outputs.PendingKey(CareProposal, uuid4()),
        pending_outputs.key(second),
    )
    with as_actor(g, care.manager):
        held = pending_outputs.ai_pending_visible(
            keys, lambda batch: batch.many(keys), clinic_id=g.clinic_a
        )
        # The unknown selector is omitted; the visible siblings keep order.
        assert [h.project(lambda c: c.record.pk) for h in held] == [
            keys[0].pk,
            keys[2].pk,
        ]
        with pytest.raises(GatewayAccessDeniedError):
            _record(held[0])
        with pytest.raises(GatewayAccessDeniedError):
            pending_outputs.VisiblePending(keys[0], g.clinic_a, object())
    refuse_plan_visit(g, care)
    with as_actor(g, care.manager):
        assert (
            pending_outputs.ai_pending_visible(
                keys, lambda batch: batch.many(keys), clinic_id=g.clinic_a
            )
            == ()
        )
        assert held[0].project(lambda c: c.record.pk) is None
