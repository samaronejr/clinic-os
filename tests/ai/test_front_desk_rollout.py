"""Exercise rollout refusals at the delivered AI-04 effects, not the evaluator."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai import front_desk
from apps.ai.contracts import GatewayRefusedError
from apps.ai.evaluation import services
from apps.ai.models import FrontDeskThread, FrontDeskTurn
from apps.ai.owner import set_global_switch, transition_model
from apps.ai.policy import check_configuration
from apps.comms import inbox_inbound
from apps.comms.inbox_inbound import inbound_context
from apps.core.secrets import secret_store
from apps.identity.models import User
from apps.intake.models import PatientAccessGrant, PatientSession
from apps.scheduling.models import Appointment
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import KEK_SECRET_NAME, protect
from django.db import DatabaseError, connection, connections, transaction

from ai.evaluation_support import as_reviewer, evaluation_world
from ai.support import model
from ai.test_front_desk import (
    Desk,
    agent_replies,
    appointment_start,
    desk_world,
    say,
    thread_of,
    turns,
    verify,
)
from ai.test_front_desk_portal import portal
from ai.test_front_desk_portal import say as portal_say
from ai.test_rollout import approve_stage, stage_world
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
REFUSALS = ["shadow", "no_autonomy", "global_kill", "source_drift"]


def refuse(desk: Desk, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Establish each forbidden exposure through the actual approval services."""
    if mode in {"shadow", "no_autonomy"}:
        world = evaluation_world(desk.graph)
        change = approve_stage(world)
        if mode == "no_autonomy":
            change = approve_stage(stage_world(world, change))
            configuration = transition_model(
                configuration_id=world.configuration.pk, state="active"
            )
            with as_reviewer(world, world.owner):
                check_configuration(configuration, world.clinic)
        with as_reviewer(world, world.owner):
            assert services.rollout_status(
                clinic_id=world.clinic, change_id=change.pk
            ) == ("shadow" if mode == "shadow" else "reviewed_live")
            assert not services.autonomy_allowed(
                clinic_id=world.clinic, configuration_id=world.configuration.pk
            )
        return
    if mode == "global_kill":
        set_global_switch(enabled=False, approved_regions=("br-synthetic",))
        return
    assert mode == "source_drift"
    original = Path.read_bytes

    def changed(path: Path) -> bytes:
        content = original(path)
        return (
            content + b"\n# synthetic front-desk source drift\n"
            if path.as_posix().endswith("apps/ai/front_desk.py")
            else content
        )

    monkeypatch.setattr(Path, "read_bytes", changed)
    with (
        runtime_role(),
        tenant_context(desk.administrator, desk.graph.organization_a),
        pytest.raises(GatewayRefusedError),
    ):
        check_configuration(desk.gateway.configuration, desk.graph.clinic_a)


def channel_grants(desk: Desk) -> tuple[int, int]:
    with owner_context(desk.graph.organization_a):
        return (
            PatientAccessGrant.objects.filter(
                issued_by_label="front-desk channel"
            ).count(),
            PatientSession.objects.filter(
                grant__issued_by_label="front-desk channel"
            ).count(),
        )


@pytest.mark.parametrize("mode", REFUSALS)
@pytest.mark.parametrize("channel", ["signed_inbound", "portal"])
def test_forbidden_exposure_keeps_inbound_messages_without_ai_effects(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    channel: str,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    session, conversation = portal(desk) if channel == "portal" else (None, None)
    with owner_context(desk.graph.organization_a):
        original = Appointment.objects.values(
            "start_at", "end_at", "status", "revision"
        ).get(pk=desk.appointment)
    refuse(desk, mode, monkeypatch)
    for text in (
        "Quero ver minha consulta",
        "02/01/2000",
        "Quero remarcar",
        "Sim",
        "Quero falar com um atendente",
    ):
        if channel == "portal":
            assert session is not None
            assert conversation is not None
            portal_say(desk, session, conversation, text)
        else:
            say(desk, text)
    assert not agent_replies(desk.graph)
    assert channel_grants(desk) == (0, 0)
    with owner_context(desk.graph.organization_a):
        assert (
            Appointment.objects.values("start_at", "end_at", "status", "revision").get(
                pk=desk.appointment
            )
            == original
        )
        assert not FrontDeskThread.objects.exclude(
            state="active",
            revision=1,
            handoff_reason="",
            challenge_attempts=0,
            challenge_until__isnull=True,
            verified_until__isnull=True,
            session_id__isnull=True,
            offered_start__isnull=True,
            offered_until__isnull=True,
            offered_turn_id__isnull=True,
            automation_after__isnull=True,
            handoff_turn_id__isnull=True,
        ).exists()
    if mode in {"shadow", "no_autonomy"}:
        proposals = turns(desk)
        assert proposals
        assert all(
            turn.state == "proposed" and turn.reply_id is None for turn in proposals
        )


def pending_acceptance(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> front_desk.Context:
    """Prove a real offered slot before pausing the runner at acceptance."""
    verify(desk)
    assert say(desk, "Quero remarcar") is not None
    assert thread_of(desk).offered_start is not None
    monkeypatch.setattr(inbox_inbound, "_RUNNERS", [])
    assert say(desk, "Sim") is None
    turn = turns(desk)[-1]
    with (
        runtime_role(),
        inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
    ):
        context = front_desk._context(
            turn.pk, secret_store().get_secret(KEK_SECRET_NAME)
        )
        assert context is not None
        assert context.offer_received
    return context


@pytest.mark.parametrize("mode", REFUSALS)
def test_python_booking_rechecks_rollout_at_the_effect(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    context = pending_acceptance(desk, monkeypatch)
    assert context.session_id is not None
    original = appointment_start(desk)
    refuse(desk, mode, monkeypatch)
    # Enter the actual booking function directly: no enqueue/runner gate runs.
    with (
        runtime_role(),
        transaction.atomic(),
    ):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_patient_session',%s,true)",
                [str(context.session_id)],
            )
        with pytest.raises(GatewayRefusedError):
            front_desk._book(context, desk.zone)
    assert appointment_start(desk) == original


def native_effect(context: front_desk.Context, effect: str, kek: str) -> object:
    with connection.cursor() as cursor:
        if effect == "state":
            cursor.execute(
                "SELECT clinic_app.ai_front_desk_state("
                "%s,'challenge',NULL,NULL,NULL,'')",
                [context.turn_id],
            )
        elif effect == "handoff":
            cursor.execute(
                "SELECT clinic_app.ai_front_desk_state("
                "%s,'handoff',NULL,NULL,NULL,'human_request')",
                [context.turn_id],
            )
        elif effect == "offer":
            cursor.execute(
                "SELECT clinic_app.ai_front_desk_state(%s,'offer',%s,%s,%s,'')",
                [
                    context.turn_id,
                    context.offered_appointment,
                    context.offered_practitioner,
                    context.offered_start,
                ],
            )
        elif effect == "verify":
            cursor.execute(
                "SELECT * FROM clinic_app.ai_front_desk_verify(%s,'2000-01-02',%s)",
                [context.turn_id, kek],
            )
        else:
            assert effect == "finish"
            body = protect(purpose="comms.message.body", plaintext=b"Synthetic reply")
            cursor.execute(
                "SELECT clinic_app.ai_front_desk_finish("
                "%s,%s,%s,'greeting','','answered',NULL)",
                [context.turn_id, body, kek],
            )
        return cursor.fetchone()


def native_authorize(context: front_desk.Context, kek: str) -> str:
    """Provide fresh application source proof; let only PostgreSQL admit it."""
    receipt = front_desk._source_receipt(context.turn_id, "turn")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.ai_front_desk_authorize(%s,%s,%s)",
            [context.turn_id, receipt, kek],
        )
        row = cursor.fetchone()
    return str(row[0]) if row and row[0] else ""


def current_turn(
    desk: Desk, context: front_desk.Context, mode: str
) -> front_desk.Context:
    if mode in {"shadow", "no_autonomy"}:
        assert say(desk, "Sim") is None
        return replace(context, turn_id=turns(desk)[-1].pk)
    return context


@pytest.mark.parametrize("mode", REFUSALS)
@pytest.mark.parametrize("effect", ["state", "handoff", "offer", "verify", "finish"])
def test_native_effect_bridges_refuse_forbidden_exposure(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    effect: str,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    context = pending_acceptance(desk, monkeypatch)
    kek = secret_store().get_secret(KEK_SECRET_NAME)
    # The verify bridge needs an open challenge, independent of turn text.
    with (
        runtime_role(),
        inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
    ):
        front_desk._state(context.turn_id, "challenge")
    # A savepoint proves the exact native call is initially admissible without
    # committing a reply, handoff, new session or a changed offer.
    with (
        runtime_role(),
        inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
        transaction.atomic(),
    ):
        assert native_authorize(context, kek) == "monitored_autonomy"
        assert native_effect(context, effect, kek) is not None
        transaction.set_rollback(True)
    original = appointment_start(desk)
    before = (channel_grants(desk), len(agent_replies(desk.graph)), thread_of(desk))
    refuse(desk, mode, monkeypatch)
    context = current_turn(desk, context, mode)
    with (
        runtime_role(),
        inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
    ):
        assert native_authorize(context, kek) == {
            "shadow": "shadow",
            "no_autonomy": "reviewed_live",
        }.get(mode, "")
        # A fresh source receipt does not grant the stage's missing permission.
        with pytest.raises(DatabaseError), transaction.atomic():
            native_effect(context, effect, kek)
    after = (channel_grants(desk), len(agent_replies(desk.graph)), thread_of(desk))
    assert after[:2] == before[:2]
    assert after[2].revision == before[2].revision
    assert appointment_start(desk) == original


@pytest.mark.parametrize("mode", REFUSALS)
def test_native_channel_booking_authority_stops_with_rollout(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    context = pending_acceptance(desk, monkeypatch)
    assert context.session_id is not None
    kek = secret_store().get_secret(KEK_SECRET_NAME)
    with (
        runtime_role(),
        inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
    ):
        assert native_authorize(context, kek) == "monitored_autonomy"
        # Use the same patient-only transition as the actual runner.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id','',true),"
                "set_config('app.current_tenant','',true),"
                "set_config('app.current_principal','',true),"
                "set_config('app.comms_inbound_proof','',true),"
                "set_config('app.current_patient_session',%s,true)",
                [str(context.session_id)],
            )
            cursor.execute(
                "SELECT count(*) FROM clinic_app.current_patient_authority('booking')"
            )
            assert cursor.fetchone() == (1,)
    refuse(desk, mode, monkeypatch)
    context = current_turn(desk, context, mode)
    with (
        runtime_role(),
        inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
    ):
        assert native_authorize(context, kek) == {
            "shadow": "shadow",
            "no_autonomy": "reviewed_live",
        }.get(mode, "")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id','',true),"
                "set_config('app.current_tenant','',true),"
                "set_config('app.current_principal','',true),"
                "set_config('app.comms_inbound_proof','',true),"
                "set_config('app.current_patient_session',%s,true)",
                [str(context.session_id)],
            )
            cursor.execute(
                "SELECT count(*) FROM clinic_app.current_patient_authority('booking')"
            )
            assert cursor.fetchone() == (0,)


@pytest.mark.parametrize("forgery", ["transaction", "subject", "guc"])
def test_native_effect_cannot_use_replayed_or_forged_source_authority(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    context = pending_acceptance(desk, monkeypatch)
    kek = secret_store().get_secret(KEK_SECRET_NAME)
    with (
        runtime_role(),
        inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
    ):
        old_receipt = front_desk._source_receipt(context.turn_id, "turn")
        assert native_authorize(context, kek) == "monitored_autonomy"
    before = len(agent_replies(desk.graph))
    with (
        runtime_role(),
        inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
        connection.cursor() as cursor,
    ):
        if forgery == "guc":
            cursor.execute(
                "SELECT set_config('app.ai_front_desk_bridge',%s,true)",
                [str(context.turn_id)],
            )
        else:
            receipt = (
                front_desk._source_receipt(uuid4(), "turn")
                if forgery == "subject"
                else old_receipt
            )
            cursor.execute(
                "SELECT clinic_app.ai_front_desk_authorize(%s,%s,%s)",
                [context.turn_id, receipt, kek],
            )
            assert cursor.fetchone() == ("",)
        with pytest.raises(DatabaseError), transaction.atomic():
            native_effect(context, "finish", kek)
    assert len(agent_replies(desk.graph)) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE clinic_app.ai_frontdeskturn "
        "SET source_xid=pg_current_xact_id()::text WHERE id=%s",
        "UPDATE clinic_app.ai_frontdeskturn "
        "SET rollout_approval_id=rollout_approval_id WHERE id=%s",
    ],
)
def test_runtime_cannot_write_its_source_or_permission_receipt(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    context = pending_acceptance(desk, monkeypatch)
    with (
        runtime_role(),
        inbound_context(account_id=desk.inbox.account, proof=desk.inbox.key),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT set_config('app.ai_front_desk_bridge',%s,true)",
            [str(context.turn_id)],
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            cursor.execute(mutation, [context.turn_id])


def test_reenable_does_not_revive_a_front_desk_turn(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    context = pending_acceptance(desk, monkeypatch)
    original = appointment_start(desk)
    before = len(agent_replies(desk.graph))
    set_global_switch(enabled=False, approved_regions=("br-synthetic",))
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    with runtime_role():
        front_desk.run_pending_turns(desk.inbox.account, desk.inbox.key)
    assert say(desk, "Sim") is None
    assert appointment_start(desk) == original
    assert len(agent_replies(desk.graph)) == before
    assert next(turn for turn in turns(desk) if turn.pk == context.turn_id).state == (
        "pending"
    )


def test_a_new_permission_does_not_rescue_an_old_queued_turn(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    context = pending_acceptance(desk, monkeypatch)
    old = turns(desk)[-1]
    original = appointment_start(desk)
    before = len(agent_replies(desk.graph))
    replacement = model(
        organization=desk.graph.organization_a, clinic=desk.graph.clinic_a
    )
    assert replacement.pk != desk.gateway.configuration.pk
    assert say(desk, "Oi") is None
    with runtime_role():
        front_desk.run_pending_turns(desk.inbox.account, desk.inbox.key)
    assert appointment_start(desk) == original
    assert len(agent_replies(desk.graph)) == before + 1
    retained = next(turn for turn in turns(desk) if turn.pk == context.turn_id)
    assert retained.state == "pending"
    assert retained.rollout_approval_id == old.rollout_approval_id
    assert turns(desk)[-1].state == "done"


def test_administrative_expansion_still_requires_platform_activation(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    world = evaluation_world(rbac_graph)
    shadow = approve_stage(world)
    live = approve_stage(stage_world(world, shadow))
    autonomy = approve_stage(stage_world(world, live))
    assert autonomy.stage == "monitored_autonomy"
    assert world.configuration.state == "approved"
    assert say(desk, "Oi") is None
    assert not turns(desk)
    assert not agent_replies(desk.graph)


def test_native_old_snapshot_cannot_reply_after_a_committed_kill(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    context = pending_acceptance(desk, monkeypatch)
    kek = secret_store().get_secret(KEK_SECRET_NAME)
    before = len(agent_replies(desk.graph))
    refused = False
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute(
            "SELECT * FROM clinic_app.comms_resolve_inbound(%s,%s)",
            [desk.inbox.account, desk.inbox.key],
        )
        account = cursor.fetchone()
        assert account is not None
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_principal',%s,true),"
            "set_config('app.comms_inbound_proof',%s,true)",
            [str(account[0]), str(account[2]), desk.inbox.key],
        )
        cursor.execute("SELECT enabled FROM clinic_app.ai_gatewayswitch WHERE id=1")
        assert cursor.fetchone() == (True,)
        # The separate owner connection commits the kill after the old
        # snapshot, before source authorization takes its shared send fence.
        with connections["locks"].cursor() as owner:
            owner.execute(
                "UPDATE clinic_app.ai_gatewayswitch SET enabled=false WHERE id=1"
            )
        cursor.execute("SELECT enabled FROM clinic_app.ai_gatewayswitch WHERE id=1")
        assert cursor.fetchone() == (True,)
        native_authorize(context, kek)
        try:
            with transaction.atomic():
                native_effect(context, "finish", kek)
        except DatabaseError:
            refused = True
    assert refused
    assert len(agent_replies(desk.graph)) == before


def _session_authorize(
    desk: Desk, context: front_desk.Context, kek: str, *, actor: UUID | None
) -> str:
    """Call the bridge on the patient-session path, optionally under a staff actor."""
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true),"
            "set_config('app.current_principal','',true),"
            "set_config('app.current_patient_session',%s,true)",
            [
                str(desk.graph.organization_a),
                "" if actor is None else str(actor),
                str(context.session_id),
            ],
        )
        return native_authorize(context, kek)


def test_staff_session_cannot_ride_the_patient_authorize_path(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = desk_world(rbac_graph, settings, monkeypatch)
    context = pending_acceptance(desk, monkeypatch)
    assert context.session_id is not None
    kek = secret_store().get_secret(KEK_SECRET_NAME)
    # Control: the verified patient session itself is admitted.
    assert _session_authorize(desk, context, kek, actor=None) == "monitored_autonomy"
    with owner_context(rbac_graph.organization_a):
        stamped = FrontDeskTurn.objects.get(pk=context.turn_id).source_xid
    assert stamped
    # The bridge reads app.current_user_id only to require it empty: the same
    # session under any staff actor, active or inactive, is refused unchanged.
    staff = desk.manager
    try:
        for active in (True, False):
            with owner_context(rbac_graph.organization_a):
                User.objects.filter(pk=staff).update(is_active=active)
            assert _session_authorize(desk, context, kek, actor=staff) == ""
            with owner_context(rbac_graph.organization_a):
                turn = FrontDeskTurn.objects.get(pk=context.turn_id)
            assert turn.source_xid == stamped
    finally:
        with owner_context(rbac_graph.organization_a):
            User.objects.filter(pk=staff).update(is_active=True)
