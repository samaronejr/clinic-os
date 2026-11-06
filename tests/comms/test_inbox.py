"""Omnichannel inbox on real PostgreSQL: windows, forged and replayed webhooks."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.comms import inbox_inbound, inbox_queries, suggestions
from apps.comms import inbox_services as services
from apps.comms.inbox_inbound import (
    InboundRejectedError,
    receive_inbound,
    verify_token_name,
)
from apps.comms.inbox_services import InboxRefusedError, Reason
from apps.comms.models import (
    Conversation,
    InboundEvent,
    IntegrationOperation,
    Message,
    MessageMeter,
    SuggestedReply,
    TemplateVersion,
)
from apps.core.integration import execute_operation
from apps.identity.models import UserClinicRole
from apps.intake.contacts import save_contact_destination, verify_contact
from django.db import DatabaseError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.utils.translation import gettext

from auth.stepup_test_support import create_role_actor
from comms.support import (
    CONTACT,
    PHONE_NUMBER_ID,
    InboxWorld,
    inbox_account,
    manager_of,
)
from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_http_support import receptionist_client
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def isolated_drafters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each scenario registers its own drafter; none leaks into another."""
    monkeypatch.setattr(suggestions, "_DRAFTERS", [])


@pytest.fixture(autouse=True)
def clinic_manager(rbac_graph: RbacGraph) -> UUID:
    """Create the clinic-A administrator before any scenario enters a context."""
    return manager_of(rbac_graph)


@pytest.fixture
def world(rbac_graph: RbacGraph, settings: SettingsWrapper) -> InboxWorld:
    return inbox_account(rbac_graph, settings)


def _only_conversation(graph: RbacGraph) -> Conversation:
    with owner_context(graph.organization_a):
        return Conversation.objects.get()


def _approved_template(graph: RbacGraph, *, category: str, purpose: str) -> UUID:
    with as_actor(graph, manager_of(graph)):
        template_id = services.create_template_version(
            clinic_id=graph.clinic_a,
            channel="whatsapp",
            name=f"modelo_{uuid4().hex[:8]}",
            language="pt_BR",
            category=category,
            purpose=purpose,
            body="Olá {{1}}, sua consulta é às {{2}}.",
        )
    with owner_context(graph.organization_a):
        for status in ("pending", "approved"):
            template = TemplateVersion.objects.get(pk=template_id)
            template.approval_status = status
            template.save(update_fields=["approval_status"])
    return template_id


def _send(
    graph: RbacGraph, conversation: UUID, **terms: object
) -> services.SendReceipt:
    with as_actor(graph, manager_of(graph)):
        return services.send_message(
            clinic_id=graph.clinic_a,
            conversation_id=conversation,
            command_id=terms.pop("command_id", uuid4()),  # type: ignore[arg-type]
            **terms,  # type: ignore[arg-type]
        )


def test_inbound_whatsapp_opens_an_unlinked_transactional_thread(
    world: InboxWorld,
) -> None:
    receipt = world.receive(world.message_body(text="Qual o horário de vocês?"))
    assert (receipt.accepted, receipt.duplicates, receipt.ignored) == (1, 0, 0)
    conversation = _only_conversation(world.graph)
    assert (conversation.purpose, conversation.state, conversation.enrollment_id) == (
        "transactional",
        "open",
        None,
    )
    assert conversation.last_inbound_at is not None
    assert not conversation.needs_clinician


def _inbound_audit(graph: RbacGraph) -> list[tuple[str, UUID, dict[str, str]]]:
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT event_type, actor_user_id, payload FROM clinic_app.audit_event "
            "WHERE event_type LIKE 'comms.inbound.%%' ORDER BY seq"
        )
        return [(row[0], row[1], json.loads(row[2])) for row in cursor.fetchall()]


def test_machine_ingest_audits_the_provider_principal_without_content(
    world: InboxWorld,
) -> None:
    world.receive(world.message_body(text="Posso tomar o remédio com café?"))
    rows = _inbound_audit(world.graph)
    assert [event for event, _, _ in rows] == [
        "comms.inbound.message_received",
        "comms.inbound.escalated",
    ]
    assert {actor for _, actor, _ in rows} == {world.principal}
    for _, _, payload in rows:
        assert set(payload) == {"clinic_id", "object_verb"}
        assert payload["clinic_id"] == str(world.graph.clinic_a)
        assert "remédio" not in str(payload)


def test_replayed_and_foreign_events_append_no_audit(world: InboxWorld) -> None:
    body = world.message_body(text="Qual o horário?", event="wamid.AUDIT")
    world.receive(body)
    after_first = _inbound_audit(world.graph)
    world.receive(body)
    world.receive(body.replace(PHONE_NUMBER_ID.encode(), b"999999999999999"))
    assert _inbound_audit(world.graph) == after_first
    assert [event for event, _, _ in after_first] == ["comms.inbound.message_received"]


def test_concurrent_first_messages_from_one_number_both_persist_once(
    world: InboxWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    barrier = threading.Barrier(2)
    original = inbox_inbound._live_conversation
    waited = threading.local()

    def lookup(
        account: inbox_inbound.ResolvedAccount, tokens: tuple[str, ...]
    ) -> Conversation | None:
        found = original(account, tokens)
        if found is None and not getattr(waited, "done", False):
            # Both deliveries see no thread before either creates one.
            waited.done = True
            barrier.wait(timeout=30)
        return found

    monkeypatch.setattr(inbox_inbound, "_live_conversation", lookup)
    receipts: list[inbox_inbound.InboundReceipt] = []
    failures: list[BaseException] = []

    def deliver(text: str) -> None:
        try:
            receipts.append(world.receive(world.message_body(text=text)))
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion
            failures.append(error)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=deliver, args=(text,))
        for text in ("Qual o horário?", "Preciso remarcar")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not failures
    assert [(r.accepted, r.duplicates, r.ignored, r.retry) for r in receipts] == [
        (1, 0, 0, 0),
        (1, 0, 0, 0),
    ]
    with owner_context(world.graph.organization_a):
        assert Conversation.objects.count() == 1
        assert Message.objects.filter(direction="in").count() == 2
        assert InboundEvent.objects.count() == 2


def test_a_status_racing_its_send_is_retried_not_consumed(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    conversation = _only_conversation(graph)
    _send(graph, conversation.pk, body="Das 8h às 18h.")
    early = world.status_body(provider_message_id="synthetic:whatsapp:x", status="sent")
    receipt = world.receive(early)
    assert (receipt.accepted, receipt.ignored, receipt.retry) == (0, 0, 1)
    with owner_context(graph.organization_a):
        assert InboundEvent.objects.filter(kind="status").count() == 0
    client = Client()
    with runtime_role():
        response = client.post(
            f"/comms/webhooks/whatsapp/{world.account}/",
            data=early,
            content_type="application/json",
            headers=world.headers(early),
        )
    assert (response.status_code, response["Retry-After"]) == (503, "5")


def test_a_status_for_no_pending_send_is_ignored_with_200(world: InboxWorld) -> None:
    receipt = world.receive(
        world.status_body(provider_message_id="wamid.OTHER", status="delivered")
    )
    assert (receipt.accepted, receipt.ignored, receipt.retry) == (0, 1, 0)


def test_forged_webhook_is_rejected_before_any_scope_query(world: InboxWorld) -> None:
    body = world.message_body(text="oi")
    with (
        runtime_role(),
        CaptureQueriesContext(connection) as queries,
        pytest.raises(InboundRejectedError),
    ):
        receive_inbound(
            provider="whatsapp",
            account_id=world.account,
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
            body=body,
        )
    assert not queries.captured_queries
    with owner_context(world.graph.organization_a):
        assert not Conversation.objects.exists()


def test_replayed_event_is_acknowledged_once_without_a_second_message(
    world: InboxWorld,
) -> None:
    body = world.message_body(text="Preciso remarcar", event="wamid.REPLAY")
    first = world.receive(body)
    replay = world.receive(body)
    assert (first.accepted, replay.accepted, replay.duplicates) == (1, 0, 1)
    with owner_context(world.graph.organization_a):
        assert Message.objects.count() == 1
        assert InboundEvent.objects.count() == 1


def test_payload_naming_another_account_or_clinic_is_ignored(
    world: InboxWorld,
) -> None:
    graph = world.graph
    foreign = world.message_body(text="oi")
    claimed = foreign.replace(PHONE_NUMBER_ID.encode(), b"999999999999999")
    receipt = world.receive(claimed)
    assert (receipt.accepted, receipt.ignored) == (0, 1)
    own = world.message_body(text="Preciso remarcar")
    stuffed = own[:-1] + f', "clinic_id": "{graph.clinic_c}"}}'.encode()
    assert world.receive(stuffed).accepted == 1
    assert _only_conversation(graph).clinic_id == graph.clinic_a


def test_window_expiry_forces_an_approved_template(
    world: InboxWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    conversation = _only_conversation(graph)
    assert _send(graph, conversation.pk, body="Atendemos das 8h às 18h.").message_id
    later = timezone.now() + timedelta(hours=24, seconds=1)
    monkeypatch.setattr(services, "timezone", SimpleNamespace(now=lambda: later))
    with pytest.raises(InboxRefusedError) as refused:
        _send(graph, conversation.pk, body="Ainda atendemos?")
    assert refused.value.code == Reason.WINDOW_EXPIRED_TEMPLATE_REQUIRED
    template = _approved_template(graph, category="utility", purpose="transactional")
    receipt = _send(
        graph,
        conversation.pk,
        template_version_id=template,
        parameters=("Ana", "10:00"),
    )
    with owner_context(graph.organization_a):
        sent = Message.objects.get(pk=receipt.message_id)
        assert sent.body == "Olá Ana, sua consulta é às 10:00."
        assert MessageMeter.objects.get(message=sent).category == "utility"


def _stale_thread(world: InboxWorld) -> Conversation:
    """A thread whose last inbound message is 25 hours old, set up as the owner."""
    graph = world.graph
    old = timezone.now() - timedelta(hours=25)
    with owner_context(graph.organization_a):
        return Conversation.objects.create(
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


def _raw_out_message(
    conversation: UUID, graph: RbacGraph, created_at: datetime
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO clinic_app.comms_message (id, organization_id, "
            "conversation_id, direction, body, sent_by_id, command_id, supported, "
            "provider_state, created_at) "
            "VALUES (%s, %s, %s, 'out', %s, %s, %s, true, '', %s)",
            [
                uuid4(),
                graph.organization_a,
                conversation,
                b"\x00",
                manager_of(graph),
                uuid4(),
                created_at,
            ],
        )


def test_the_database_refuses_free_text_after_the_window(world: InboxWorld) -> None:
    graph = world.graph
    stale = _stale_thread(world)
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        Message.objects.create(
            organization_id=graph.organization_a,
            conversation=stale,
            direction="out",
            body="fora da janela",
            sent_by_id=manager_of(graph),
            command_id=uuid4(),
            created_at=timezone.now(),
        )


def test_a_back_dated_created_at_cannot_reopen_the_window(world: InboxWorld) -> None:
    graph = world.graph
    # Control: the same raw statement, with every NOT NULL column, is accepted on
    # a thread whose window is genuinely open, so the refusal below can only come
    # from the window rule and not from a malformed row.
    world.receive(world.message_body(text="Qual o horário?"))
    open_thread = _only_conversation(graph)
    with as_actor(graph, manager_of(graph)), transaction.atomic():
        _raw_out_message(open_thread.pk, graph, timezone.now())
    with owner_context(graph.organization_a):
        assert Message.objects.filter(direction="out").count() == 1
    stale = _stale_thread(world)
    assert stale.last_inbound_at is not None
    # The claimed time is one minute after the stale inbound message, well inside
    # the 24 hour window. Judged by this claim the row would be allowed; it is
    # refused only because the database overwrites created_at with its own clock.
    claimed = stale.last_inbound_at + timedelta(minutes=1)
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(DatabaseError) as refused,
        transaction.atomic(),
    ):
        _raw_out_message(stale.pk, graph, claimed)
    assert getattr(refused.value.__cause__, "sqlstate", None) == "23514"
    assert "service window closed" in str(refused.value)
    with owner_context(graph.organization_a):
        assert Message.objects.filter(conversation_id=stale.pk).count() == 0


def test_the_runtime_role_cannot_advance_last_inbound_at(world: InboxWorld) -> None:
    graph = world.graph
    stale = _stale_thread(world)
    for statement, params in (
        (
            "UPDATE clinic_app.comms_conversation SET last_inbound_at = now() "
            "WHERE id = %s",
            [stale.pk],
        ),
        (
            "UPDATE clinic_app.comms_conversation SET last_inbound_at = NULL "
            "WHERE id = %s",
            [stale.pk],
        ),
    ):
        with (
            as_actor(graph, manager_of(graph)),
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(statement, params)
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(DatabaseError),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT clinic_app.comms_open_window(%s)", [stale.pk])
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        Conversation.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            provider_account_id=world.account,
            channel="whatsapp",
            purpose="transactional",
            contact_fingerprint=uuid4().hex + uuid4().hex,
            contact_address="+5511999990999",
            last_inbound_at=timezone.now(),
            last_activity_at=timezone.now(),
        )
    with owner_context(graph.organization_a):
        assert Conversation.objects.get(pk=stale.pk).last_inbound_at == (
            stale.last_inbound_at
        )


def test_only_a_proven_inbound_message_opens_the_window(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    conversation = _only_conversation(graph)
    with connection.cursor() as cursor:
        cursor.execute("SELECT statement_timestamp()")
        database_now = cursor.fetchone()[0]
    assert conversation.last_inbound_at is not None
    assert abs(database_now - conversation.last_inbound_at) < timedelta(minutes=1)


def _draft_template(graph: RbacGraph, channel: str) -> UUID:
    with as_actor(graph, manager_of(graph)):
        return services.create_template_version(
            clinic_id=graph.clinic_a,
            channel=channel,
            name=f"modelo_{uuid4().hex[:8]}",
            language="pt_BR",
            category="utility",
            purpose="transactional",
            body="Olá {{1}}.",
        )


def test_a_template_cannot_be_inserted_already_approved(world: InboxWorld) -> None:
    graph = world.graph
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        TemplateVersion.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            channel="whatsapp",
            name="inserido_aprovado",
            language="pt_BR",
            version=1,
            category="utility",
            purpose="transactional",
            body="Olá {{1}}.",
            parameter_count=1,
            content_digest="0" * 64,
            approval_status="approved",
            created_by_id=manager_of(graph),
        )


def test_staff_cannot_approve_a_whatsapp_template_at_the_database(
    world: InboxWorld,
) -> None:
    graph = world.graph
    template = _draft_template(graph, "whatsapp")
    with owner_context(graph.organization_a):
        TemplateVersion.objects.filter(pk=template).update(approval_status="pending")
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(DatabaseError),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "UPDATE clinic_app.comms_templateversion SET approval_status = 'approved' "
            "WHERE id = %s",
            [template],
        )
    with owner_context(graph.organization_a):
        assert TemplateVersion.objects.get(pk=template).approval_status == "pending"


def test_only_the_provider_status_event_approves_a_whatsapp_template(
    world: InboxWorld,
) -> None:
    graph = world.graph
    template = _draft_template(graph, "whatsapp")
    with owner_context(graph.organization_a):
        row = TemplateVersion.objects.get(pk=template)
        TemplateVersion.objects.filter(pk=template).update(approval_status="pending")
    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-1",
                    "changes": [
                        {
                            "field": "message_template_status_update",
                            "value": {
                                "event": "APPROVED",
                                "message_template_id": 7,
                                "message_template_name": row.name,
                                "message_template_language": "pt_BR",
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()
    assert world.receive(body).accepted == 1
    with owner_context(graph.organization_a):
        approved = TemplateVersion.objects.get(pk=template)
        assert approved.approval_status == "approved"
        assert approved.approval_changed_at is not None


def test_a_manager_approves_an_email_template_through_the_service(
    world: InboxWorld,
) -> None:
    graph = world.graph
    template = _draft_template(graph, "email")
    for status in ("pending", "approved"):
        with as_actor(graph, manager_of(graph)):
            services.set_template_approval(
                clinic_id=graph.clinic_a, template_version_id=template, status=status
            )
    with owner_context(graph.organization_a):
        assert TemplateVersion.objects.get(pk=template).approval_status == "approved"


def test_marketing_never_enters_a_transactional_thread(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    conversation = _only_conversation(graph)
    marketing = _approved_template(graph, category="marketing", purpose="marketing")
    with pytest.raises(InboxRefusedError) as refused:
        _send(
            graph,
            conversation.pk,
            template_version_id=marketing,
            parameters=("Ana", "10:00"),
        )
    assert refused.value.code == Reason.TEMPLATE_PURPOSE_MISMATCH
    with owner_context(graph.organization_a), pytest.raises(DatabaseError):
        Conversation.objects.filter(pk=conversation.pk).update(purpose="marketing")
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        Message.objects.create(
            organization_id=graph.organization_a,
            conversation=conversation,
            direction="out",
            body="oferta",
            template_id=marketing,
            sent_by_id=manager_of(graph),
            command_id=uuid4(),
            created_at=timezone.now(),
        )


def test_a_marketing_thread_needs_a_consent_source_and_a_template(
    rbac_graph: RbacGraph, world: InboxWorld
) -> None:
    chart = chart_world(rbac_graph)
    with (
        as_actor(rbac_graph, manager_of(rbac_graph)),
        pytest.raises(InboxRefusedError) as refused,
    ):
        services.open_conversation(
            clinic_id=rbac_graph.clinic_a,
            enrollment_id=chart.enrollment,
            channel="whatsapp",
            purpose="marketing",
        )
    assert refused.value.code == Reason.MARKETING_OPT_IN_MISSING


def test_provider_outage_keeps_one_message_and_never_duplicates_on_recovery(
    world: InboxWorld, settings: SettingsWrapper
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    conversation = _only_conversation(graph)
    receipt = _send(graph, conversation.pk, body="Das 8h às 18h.")
    settings.COMMS_SYNTHETIC_FAILURE_CHANNELS = ("whatsapp",)
    with owner_context(graph.organization_a):
        operation = IntegrationOperation.objects.get(subject_id=receipt.message_id)
    with runtime_role():
        assert execute_operation(operation.pk) == "retry"
    settings.COMMS_SYNTHETIC_FAILURE_CHANNELS = ()
    with runtime_role():
        assert execute_operation(operation.pk) == "succeeded"
        assert execute_operation(operation.pk) == "skipped"
    with owner_context(graph.organization_a):
        assert Message.objects.filter(direction="out").count() == 1
        sent = IntegrationOperation.objects.get(pk=operation.pk)
        assert sent.provider_reference == f"synthetic:whatsapp:{operation.pk}"
        assert sent.attempt_count == 2


def _template_form(html: str, template: UUID) -> str:
    return html.split(f'data-template="{template}"')[1].split("</form>", maxsplit=1)[0]


def test_the_page_renders_one_input_per_placeholder_and_sends_the_template(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    conversation = _only_conversation(graph)
    template = _approved_template(graph, category="utility", purpose="transactional")
    client, _ = receptionist_client(graph)
    url = f"/clinics/{graph.clinic_a}/messages/{conversation.pk}/"
    with runtime_role():
        form = _template_form(client.get(url).content.decode(), template)
    assert re.findall(r'name="(p\d+)"', form) == ["p1", "p2"]
    with runtime_role():
        sent = client.post(
            url,
            {
                "action": "send",
                "command_id": str(uuid4()),
                "template_version_id": str(template),
                "p1": "Ana",
                "p2": "10:00",
                # An unrequested extra value is ignored; the stored count rules.
                "p3": "ignorado",
                "parameter_count": "3",
            },
        )
    assert sent.status_code == 302
    with owner_context(graph.organization_a):
        body = Message.objects.get(direction="out").body
    assert body == "Olá Ana, sua consulta é às 10:00."


def test_a_template_post_with_the_wrong_parameter_count_is_refused(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    conversation = _only_conversation(graph)
    template = _approved_template(graph, category="utility", purpose="transactional")
    client, _ = receptionist_client(graph)
    url = f"/clinics/{graph.clinic_a}/messages/{conversation.pk}/"
    with runtime_role():
        refused = client.post(
            url,
            {
                "action": "send",
                "command_id": str(uuid4()),
                "template_version_id": str(template),
                "p1": "Ana",
                "parameter_count": "2",
            },
            follow=True,
        )
    assert gettext("Review the text or the template parameters.") in (
        refused.content.decode()
    )
    with owner_context(graph.organization_a):
        assert not Message.objects.filter(direction="out").exists()
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(InboxRefusedError) as short,
    ):
        services.send_message(
            clinic_id=graph.clinic_a,
            conversation_id=conversation.pk,
            command_id=uuid4(),
            template_version_id=template,
            parameters=("Ana",),
        )
    assert short.value.code == Reason.INVALID_BODY


def test_a_repeated_command_returns_the_original_receipt(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    conversation = _only_conversation(graph)
    command = uuid4()
    first = _send(graph, conversation.pk, body="Das 8h às 18h.", command_id=command)
    again = _send(graph, conversation.pk, body="Das 8h às 18h.", command_id=command)
    assert (again.message_id, again.replayed) == (first.message_id, True)
    with pytest.raises(services.InboxConflictError):
        _send(graph, conversation.pk, body="Outro texto.", command_id=command)
    with owner_context(graph.organization_a):
        assert (
            IntegrationOperation.objects.filter(subject_type="comms.message").count()
            == 1
        )


def test_status_webhook_advances_delivery_and_records_the_providers_meter(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    conversation = _only_conversation(graph)
    receipt = _send(graph, conversation.pk, body="Das 8h às 18h.")
    with owner_context(graph.organization_a):
        operation = IntegrationOperation.objects.get(subject_id=receipt.message_id)
    with runtime_role():
        execute_operation(operation.pk)
    reference = f"synthetic:whatsapp:{operation.pk}"
    delivered = world.status_body(provider_message_id=reference, status="delivered")
    assert world.receive(delivered).accepted == 1
    assert world.receive(delivered).duplicates == 1
    with as_actor(graph, manager_of(graph)):
        assert services.message_states(
            [Message.objects.get(pk=receipt.message_id)]
        ) == {receipt.message_id: "delivered"}
    with owner_context(graph.organization_a):
        meter = MessageMeter.objects.get(
            message_id=receipt.message_id, source="provider"
        )
        assert (meter.category, meter.billable) == ("utility", True)


def test_a_clinical_question_is_flagged_for_a_clinician_and_gets_no_draft(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Posso tomar o remédio com café?"))
    conversation = _only_conversation(graph)
    assert (conversation.needs_clinician, conversation.triage_category) == (
        True,
        "clinical",
    )
    suggestions.register_reply_drafter(
        "synthetic", lambda _request: "Retornamos em breve."
    )
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(InboxRefusedError) as refused,
    ):
        suggestions.request_suggestion(
            clinic_id=graph.clinic_a, conversation_id=conversation.pk
        )
    assert refused.value.code == Reason.AI_NOT_ALLOWED


def test_a_suggestion_is_inert_until_a_human_sends_it(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário de funcionamento?"))
    conversation = _only_conversation(graph)
    suggestions.register_reply_drafter(
        "synthetic", lambda _request: "Atendemos das 8h às 18h."
    )
    with as_actor(graph, manager_of(graph)):
        suggestion_id = suggestions.request_suggestion(
            clinic_id=graph.clinic_a, conversation_id=conversation.pk
        )
    with owner_context(graph.organization_a):
        assert SuggestedReply.objects.get(pk=suggestion_id).state == "proposed"
        assert not Message.objects.filter(direction="out").exists()
        assert not IntegrationOperation.objects.filter(
            subject_type="comms.message"
        ).exists()
    with as_actor(graph, manager_of(graph)):
        receipt = suggestions.use_suggestion(
            clinic_id=graph.clinic_a,
            suggestion_id=suggestion_id,
            command_id=uuid4(),
            body="Atendemos das 8h às 18h, de segunda a sexta.",
        )
    with owner_context(graph.organization_a):
        used = SuggestedReply.objects.get(pk=suggestion_id)
        assert (used.state, used.used_message_id) == ("edited", receipt.message_id)


def test_a_drifting_draft_is_dropped_before_storage(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário de funcionamento?"))
    conversation = _only_conversation(graph)
    suggestions.register_reply_drafter(
        "synthetic", lambda _request: "Tome a dose dobrada."
    )
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(InboxRefusedError) as refused,
    ):
        suggestions.request_suggestion(
            clinic_id=graph.clinic_a, conversation_id=conversation.pk
        )
    assert refused.value.code == Reason.AI_DROPPED
    with owner_context(graph.organization_a):
        assert not SuggestedReply.objects.exists()


def test_contact_fingerprint_is_stable_across_a_secret_key_change(
    world: InboxWorld, settings: SettingsWrapper
) -> None:
    graph = world.graph
    address = f"+{CONTACT}"
    with as_actor(graph, manager_of(graph)):
        before = services.contact_fingerprints(channel="whatsapp", address=address)
    settings.SECRET_KEY = "a-different-application-secret-" + "x" * 32
    with as_actor(graph, manager_of(graph)):
        after = services.contact_fingerprints(channel="whatsapp", address=address)
        other_channel = services.contact_fingerprints(channel="sms", address=address)
    assert before == after
    assert other_channel != after
    # A thread opened before the change is still the live thread afterwards.
    world.receive(world.message_body(text="Qual o horário?"))
    settings.SECRET_KEY = "yet-another-application-secret-" + "y" * 32
    world.receive(world.message_body(text="Ainda aí?"))
    with owner_context(graph.organization_a):
        assert Conversation.objects.count() == 1
        assert Message.objects.filter(direction="in").count() == 2


def test_linking_needs_the_patients_verified_contact(world: InboxWorld) -> None:
    graph = world.graph
    chart = chart_world(graph)
    world.receive(world.message_body(text="Qual o horário?"))
    conversation = _only_conversation(graph)
    with as_actor(graph, manager_of(graph)):
        save_contact_destination(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            channel="whatsapp",
            destination=f"+{CONTACT}",
            expected_version=None,
        )
        with pytest.raises(InboxRefusedError) as unverified:
            services.link_conversation_to_patient(
                clinic_id=graph.clinic_a,
                conversation_id=conversation.pk,
                enrollment_id=chart.enrollment,
                expected_revision=conversation.revision,
            )
        assert unverified.value.code == Reason.CONTACT_UNVERIFIED
        verify_contact(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            channel="whatsapp",
            expected_version=1,
        )
        services.link_conversation_to_patient(
            clinic_id=graph.clinic_a,
            conversation_id=conversation.pk,
            enrollment_id=chart.enrollment,
            expected_revision=conversation.revision,
        )
    assert _only_conversation(graph).enrollment_id == chart.enrollment


def test_a_different_number_cannot_be_linked_to_the_patient(world: InboxWorld) -> None:
    graph = world.graph
    chart = chart_world(graph)
    world.receive(world.message_body(text="Qual o horário?", sender="5511988880000"))
    conversation = _only_conversation(graph)
    with as_actor(graph, manager_of(graph)):
        save_contact_destination(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            channel="whatsapp",
            destination=f"+{CONTACT}",
            expected_version=None,
        )
        verify_contact(
            clinic_id=graph.clinic_a,
            enrollment_id=chart.enrollment,
            channel="whatsapp",
            expected_version=1,
        )
        with pytest.raises(InboxRefusedError) as refused:
            services.link_conversation_to_patient(
                clinic_id=graph.clinic_a,
                conversation_id=conversation.pk,
                enrollment_id=chart.enrollment,
                expected_revision=conversation.revision,
            )
    assert refused.value.code == Reason.CONTACT_MISMATCH


def test_a_post_to_an_unknown_conversation_is_a_plain_404_and_changes_nothing(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Qual o horário?"))
    before = _only_conversation(graph)
    client, _ = receptionist_client(graph)
    url = f"/clinics/{graph.clinic_a}/messages/{uuid4()}/"
    with runtime_role():
        refused = client.post(
            url,
            {"action": "assign", "assignee": "me", "expected_revision": "1"},
        )
    assert refused.status_code == HTTPStatus.NOT_FOUND
    with owner_context(graph.organization_a):
        after = Conversation.objects.get()
        assert (after.pk, after.revision, after.assignee_id) == (
            before.pk,
            before.revision,
            before.assignee_id,
        )


def test_the_list_hides_threads_whose_purpose_the_role_may_not_handle(
    world: InboxWorld,
) -> None:
    graph = world.graph
    now = timezone.now()
    with owner_context(graph.organization_a):
        for purpose in ("transactional", "marketing"):
            Conversation.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                provider_account_id=world.account,
                channel="whatsapp",
                purpose=purpose,
                contact_fingerprint=uuid4().hex + uuid4().hex,
                contact_address=f"+5511999{purpose[:2]}",
                last_activity_at=now,
            )
    receptionist = create_role_actor(graph, UserClinicRole.Role.RECEPTIONIST)
    with as_actor(graph, receptionist.pk):
        reception = inbox_queries.list_conversations(clinic_id=graph.clinic_a)
    with as_actor(graph, manager_of(graph)):
        management = inbox_queries.list_conversations(clinic_id=graph.clinic_a)
    # Marketing is the managers' purpose: reception never lists or opens it.
    assert {row.conversation.purpose for row in reception} == {"transactional"}
    assert {row.conversation.purpose for row in management} == {
        "transactional",
        "marketing",
    }


def test_webhook_endpoint_needs_no_session_and_answers_the_handshake(
    world: InboxWorld, settings: SettingsWrapper
) -> None:
    root = Path(str(settings.CLINIC_SECRET_DIR))
    path = root / f"{verify_token_name(world.account)}.secret"
    path.write_text("ab" * 32)
    path.chmod(0o600)
    client = Client(enforce_csrf_checks=True)
    url = f"/comms/webhooks/whatsapp/{world.account}/"
    body = world.message_body(text="Qual o horário?")
    with runtime_role():
        ok = client.post(
            url,
            data=body,
            content_type="application/json",
            headers=world.headers(body),
        )
        forged = client.post(
            url,
            data=body,
            content_type="application/json",
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )
        challenge = client.get(
            url,
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "ab" * 32,
                "hub.challenge": "4242",
            },
        )
        wrong = client.get(
            url,
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "nope",
                "hub.challenge": "4242",
            },
        )
    assert (ok.status_code, forged.status_code) == (200, 403)
    assert (challenge.status_code, challenge.content, wrong.status_code) == (
        200,
        b"4242",
        403,
    )
