"""Contact invalidation and publication races on the real channel authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai import front_desk
from apps.ai.models import FrontDeskTurn
from apps.comms.inbox_inbound import authenticate
from apps.comms.models import Message
from apps.core.secrets import secret_store
from apps.intake.contacts import save_contact_destination
from apps.intake.models import PatientClinicEnrollment, PatientSession
from apps.scheduling.models import Appointment
from apps.tenancy.envelope import KEK_SECRET_NAME
from django.db import DatabaseError, connection, connections

from ai.test_front_desk import (
    TOOLS,
    _agent_policy,
    appointment_start,
    desk_world,
    say,
    thread_of,
    turns,
    verify,
    when,
)
from ai.test_front_desk_regressions import no_upcoming
from comms.support import inbox_account
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_contact_invalidation_after_cached_read_cannot_overtake_publication(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    verify(desk)
    detail = when(appointment_start(desk), desk.zone)
    retrieved, probed, invalidated = Event(), Event(), Event()
    blocked: list[bool] = []
    published: list[tuple[bool, bool]] = []
    original_step, original_finish = front_desk._appointment_step, front_desk._finish

    def pause_after_retrieval(
        ctx: front_desk.Context, decision: front_desk.Decision
    ) -> front_desk.Decision:
        result = original_step(ctx, decision)
        assert detail in result.reply
        retrieved.set()
        assert probed.wait(timeout=30)
        if not blocked[0]:
            # An unprotected publication interval: commit the real mutation
            # before the cached answer is allowed to reach the finish bridge.
            assert invalidated.wait(timeout=30)
        return result

    def observe_publication(
        ctx: front_desk.Context, decision: front_desk.Decision, kek: str
    ) -> None:
        original_finish(ctx, decision, kek)
        published.append((invalidated.is_set(), detail in decision.reply))

    def probe_mutation_key(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object] | None,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if (
            "pg_catalog.pg_advisory_xact_lock(" in sql
            and params
            and not blocked
            and (
                str(params[0]) == f"clinic-lock-v1:clinic:{rbac_graph.clinic_a}"
                or str(params[0]).startswith(
                    "clinic-lock-v1:comms-subject:intake.patient_channel_preference:"
                )
            )
        ):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))",
                    [str(params[0])],
                )
                blocked.append(cursor.fetchone()[0] is False)
            probed.set()
        return execute(sql, params, many, context)

    def change_contact() -> None:
        try:
            with (
                as_actor(rbac_graph, desk.manager),
                connection.execute_wrapper(probe_mutation_key),
            ):
                save_contact_destination(
                    clinic_id=rbac_graph.clinic_a,
                    enrollment_id=desk.enrollment,
                    channel="whatsapp",
                    destination="+5511999990002",
                    expected_version=1,
                )
            invalidated.set()
        finally:
            connections.close_all()

    def read_appointment() -> str | None:
        try:
            return say(desk, "Quando é minha consulta?")
        finally:
            connections.close_all()

    with monkeypatch.context() as patch:
        patch.setattr(front_desk, "_appointment_step", pause_after_retrieval)
        patch.setattr(front_desk, "_finish", observe_publication)
        with ThreadPoolExecutor(max_workers=2) as pool:
            reply = pool.submit(read_appointment)
            assert retrieved.wait(timeout=30)
            mutation = pool.submit(change_contact)
            reply.result(timeout=40)
            mutation.result(timeout=40)
    # A legitimate mutation either wins and suppresses the stale answer, or
    # waits until the authorized answer commits. It cannot overtake insertion.
    assert {
        "invalidation_committed": invalidated.is_set(),
        "mutation_was_blocked": blocked,
    } == {"invalidation_committed": True, "mutation_was_blocked": [True]}
    assert not any(changed and disclosed for changed, disclosed in published)
    # Once invalidation committed, a fresh request discloses no appointment.
    refused = say(desk, "Quando é minha consulta?")
    assert refused is not None
    assert detail not in refused


@pytest.mark.parametrize("new_booking", [False, True])
def test_offer_expiry_at_publication_rolls_back_appointment_and_receipt(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    new_booking: bool,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    original = appointment_start(desk)
    if new_booking:
        no_upcoming(desk, available=True)
    verify(desk)
    say(desk, "Quero agendar" if new_booking else "Quero remarcar")
    thread = thread_of(desk)
    offered = thread.offered_start
    assert offered is not None
    original_finish, original_patient = front_desk._finish, front_desk._under_session

    def patient_scope(
        ctx: front_desk.Context, decision: front_desk.Decision
    ) -> front_desk.Decision:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user,current_setting('app.current_user_id',true),"
                "current_setting('app.current_tenant',true),"
                "current_setting('app.current_principal',true),"
                "current_setting('app.current_patient_session',true)"
            )
            row = cursor.fetchone()
        assert row[0] == "clinic_app"
        assert all(value in (None, "") for value in row[1:4])
        assert row[4] == str(ctx.session_id)
        return original_patient(ctx, decision)

    def expire_before_finish(
        ctx: front_desk.Context, decision: front_desk.Decision, kek: str
    ) -> None:
        # Owner fixture clock adjustment on this same transaction; the actual
        # writer and finish bridge before/after it still execute as clinic_app.
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_owner")
            cursor.execute(
                "SELECT set_config('app.ai_front_desk_bridge',%s,true)",
                [str(thread.pk)],
            )
            cursor.execute(
                "UPDATE clinic_app.ai_frontdeskthread SET "
                "offered_until=statement_timestamp()-interval '1 second' WHERE id=%s",
                [thread.pk],
            )
            cursor.execute("SELECT set_config('app.ai_front_desk_bridge','',true)")
            cursor.execute("SET LOCAL ROLE clinic_app")
        original_finish(ctx, decision, kek)

    body = desk.inbox.message_body(text="SIM")
    with monkeypatch.context() as patch:
        patch.setattr(front_desk, "_under_session", patient_scope)
        patch.setattr(front_desk, "_finish", expire_before_finish)
        with pytest.raises(DatabaseError) as refused:
            desk.inbox.receive(body)
    assert getattr(refused.value.__cause__, "sqlstate", None) == "42501"
    with owner_context(rbac_graph.organization_a):
        patient = PatientClinicEnrollment.objects.get(pk=desk.enrollment).patient_id
        if new_booking:
            assert not Appointment.objects.filter(
                patient_id=patient, status="scheduled"
            ).exists()
        else:
            assert Appointment.objects.get(pk=desk.appointment).start_at == original
    assert turns(desk)[-1].reply_id is None
    # The inbound acceptance remains pending; retry the same command once the
    # injected expiry is gone. Both effect and provenance commit exactly once.
    proof = authenticate(
        account_id=desk.inbox.account, headers=desk.inbox.headers(body), body=body
    )
    with runtime_role():
        front_desk.run_pending_turns(desk.inbox.account, proof)
    with owner_context(rbac_graph.organization_a):
        booked = Appointment.objects.get(patient_id=patient, status="scheduled")
    assert booked.start_at == offered
    assert turns(desk)[-1].outcome == "booked"
    assert sum(turn.tool == "scheduling.book_approved" for turn in turns(desk)) == 1


def _gucs() -> tuple[str | None, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('app.current_user_id',true),"
            "current_setting('app.current_tenant',true),"
            "current_setting('app.current_principal',true),"
            "current_setting('app.current_patient_session',true),"
            "current_setting('app.comms_inbound_proof',true)"
        )
        return tuple(cursor.fetchone())


def test_channel_patient_scope_restores_inbound_gucs_before_and_after_reply(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    verify(desk)
    original_patient, original_finish = front_desk._under_session, front_desk._finish
    scopes: list[tuple[str | None, ...]] = []

    def patient(
        ctx: front_desk.Context, decision: front_desk.Decision
    ) -> front_desk.Decision:
        current = _gucs()
        assert all(value in (None, "") for value in current[:3])
        assert current[3] == str(ctx.session_id)
        assert current[4] in (None, "")
        return original_patient(ctx, decision)

    def finish(
        ctx: front_desk.Context, decision: front_desk.Decision, kek: str
    ) -> None:
        before = _gucs()
        assert before[0] in (None, "")
        assert before[1] == str(rbac_graph.organization_a)
        assert before[2] == str(desk.inbox.principal)
        assert before[3] in (None, "")
        assert before[4] == desk.inbox.key
        original_finish(ctx, decision, kek)
        assert _gucs() == before
        scopes.append(before)

    with monkeypatch.context() as patch:
        patch.setattr(front_desk, "_under_session", patient)
        patch.setattr(front_desk, "_finish", finish)
        assert say(desk, "Quando é minha consulta?") is not None
    assert len(scopes) == 1
    assert all(value in (None, "") for value in _gucs()[:4])


def test_merged_runner_unknown_and_wrong_clinic_turns_have_identical_denial(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    other = replace(rbac_graph, clinic_a=rbac_graph.clinic_b)
    foreign = inbox_account(
        other, settings, phone_number_id=f"1{uuid4().int % 10**14:014d}"
    )
    _agent_policy(other, rbac_graph.clinic_admin, TOOLS)
    with monkeypatch.context() as patch:
        patch.setattr(front_desk, "_run", lambda *args: None)
        foreign.receive(foreign.message_body(text="Oi"))
    with owner_context(rbac_graph.organization_a):
        foreign_turn = FrontDeskTurn.objects.get(clinic_id=rbac_graph.clinic_b)
        before = (
            FrontDeskTurn.objects.count(),
            Message.objects.count(),
            PatientSession.objects.count(),
            Appointment.objects.count(),
        )
    kek = secret_store().get_secret(KEK_SECRET_NAME)

    def forbidden_patient_path(
        ctx: front_desk.Context, decision: front_desk.Decision
    ) -> front_desk.Decision:
        pytest.fail("unowned turn reached the patient path")

    with monkeypatch.context() as patch:
        patch.setattr(front_desk, "_appointment_step", forbidden_patient_path)
        with runtime_role():
            for selector in (uuid4(), foreign_turn.pk):
                front_desk._run(desk.inbox.account, desk.inbox.key, selector, kek)
    with owner_context(rbac_graph.organization_a):
        assert (
            FrontDeskTurn.objects.count(),
            Message.objects.count(),
            PatientSession.objects.count(),
            Appointment.objects.count(),
        ) == before
