"""Omnichannel inbox on real PostgreSQL: windows, forged and replayed webhooks."""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from apps.audit.models import AuditEvent
from apps.comms import inbox_adapters, inbox_inbound, inbox_queries, suggestions
from apps.comms import inbox_services as services
from apps.comms.adapters import PermanentSendError, TransientSendError
from apps.comms.inbox_inbound import (
    InboundRejectedError,
    receive_inbound,
    verify_token_name,
)
from apps.comms.inbox_services import PROVIDER_BY_CHANNEL, InboxRefusedError, Reason
from apps.comms.migrations._permit_evidence_sql import (
    BACKFILL_SQL as PERMIT_EVIDENCE_BACKFILL,
)
from apps.comms.models import (
    Conversation,
    InboundEvent,
    IntegrationOperation,
    Message,
    MessageMeter,
    ProviderAccount,
    SuggestedReply,
    TemplateVersion,
)
from apps.core import integration
from apps.core.integration import (
    OperationRequest,
    enqueue_operation,
    execute_operation,
)
from apps.identity.models import UserClinicRole
from apps.intake.contacts import save_contact_destination, verify_contact
from django.db import (
    DatabaseError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    connection,
    connections,
    transaction,
)
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
    from collections.abc import Callable, Iterator
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


def test_late_status_belongs_to_its_provider_attempt(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id

    def send_attempt() -> str:
        with owner_context(graph.organization_a):
            operation = IntegrationOperation.objects.get(
                subject_id=message_id, status="pending"
            )
        with runtime_role():
            assert execute_operation(operation.pk) == "succeeded"
        with owner_context(graph.organization_a):
            operation.refresh_from_db()
        assert operation.provider_reference
        return operation.provider_reference

    def state() -> str:
        with as_actor(graph, manager_of(graph)):
            return services.message_states([Message.objects.get(pk=message_id)])[
                message_id
            ]

    def retry() -> None:
        with as_actor(graph, manager_of(graph)):
            services.retry_message(
                clinic_id=graph.clinic_a, message_id=message_id, command_id=uuid4()
            )

    first = send_attempt()
    failed = world.status_body(provider_message_id=first, status="failed")
    assert world.receive(failed).accepted == 1
    assert state() == "failed"
    retry()
    second = send_attempt()
    assert second != first
    assert state() == "sent"
    assert world.receive(failed.replace(b"1790000000", b"1790000001")).accepted == 1
    assert state() == "sent"
    with pytest.raises(InboxRefusedError, match=Reason.NOT_FAILED):
        retry()
    assert (
        world.receive(
            world.status_body(provider_message_id=first, status="read")
        ).accepted
        == 1
    )
    assert (
        world.receive(
            world.status_body(provider_message_id=second, status="failed")
        ).accepted
        == 1
    )
    assert state() == "read"
    with pytest.raises(InboxRefusedError, match=Reason.NOT_FAILED):
        retry()
    with owner_context(graph.organization_a):
        assert IntegrationOperation.objects.filter(subject_id=message_id).count() == 2


def test_accepted_retry_stays_current_when_its_worker_clock_runs_behind(
    world: InboxWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        first = IntegrationOperation.objects.get(subject_id=message_id)
    with runtime_role():
        assert execute_operation(first.pk) == "succeeded"
    with owner_context(graph.organization_a):
        first.refresh_from_db()
    assert first.provider_reference
    failed = world.status_body(
        provider_message_id=first.provider_reference, status="failed"
    )
    assert world.receive(failed).accepted == 1

    def retry() -> None:
        with as_actor(graph, manager_of(graph)):
            services.retry_message(
                clinic_id=graph.clinic_a, message_id=message_id, command_id=uuid4()
            )

    # The retrying web worker's clock runs ten seconds behind the first sender's.
    created_at = IntegrationOperation._meta.get_field("created_at")
    behind = first.created_at - timedelta(seconds=10)

    def behind_clock(instance: IntegrationOperation, *, add: bool) -> datetime:
        assert add
        instance.created_at = behind
        return behind

    with monkeypatch.context() as clock:
        clock.setattr(created_at, "pre_save", behind_clock)
        retry()
    with owner_context(graph.organization_a):
        second = IntegrationOperation.objects.get(
            subject_id=message_id, status="pending"
        )
    assert second.created_at < first.created_at
    with runtime_role():
        assert execute_operation(second.pk) == "succeeded"
    with owner_context(graph.organization_a):
        second.refresh_from_db()
    assert second.provider_reference not in {None, first.provider_reference}
    assert world.receive(failed).duplicates == 1
    late = world.receive(failed.replace(b"1790000000", b"1790000001"))
    assert late.accepted == 1
    with as_actor(graph, manager_of(graph)):
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "sent"
        }
    with pytest.raises(InboxRefusedError, match=Reason.NOT_FAILED):
        retry()
    with owner_context(graph.organization_a):
        assert IntegrationOperation.objects.filter(subject_id=message_id).count() == 2


def _dispatch_pending(graph: RbacGraph, message_id: UUID) -> str:
    with owner_context(graph.organization_a):
        operation = IntegrationOperation.objects.get(
            subject_id=message_id, status="pending"
        )
    with runtime_role():
        assert execute_operation(operation.pk) == "succeeded"
    with owner_context(graph.organization_a):
        operation.refresh_from_db()
    assert operation.provider_reference
    return operation.provider_reference


def _refusal(error: IntegrityError) -> str | None:
    cause = error.__cause__
    diag = getattr(cause, "diag", None)
    return getattr(diag, "constraint_name", None)


def test_send_permit_is_claimed_only_through_its_resolver(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    _dispatch_pending(graph, message_id)
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    assert attempt.send_permit == "held"

    claim_sql = "SELECT clinic_app.comms_claim_send_permit(%s)"

    def claim(operation_id: UUID) -> str:
        return str(
            _value(graph.organization_a, claim_sql, [str(operation_id)], lock=True)
        )

    # Only the lock session may ask at all.
    assert _forge(graph.organization_a, claim_sql, [str(attempt.pk)]) == "42501"
    # Outside the worker's session lock nothing is claimed; an unknown attempt
    # is refused like any other.
    # An accepted attempt is settled: it can never be claimed again.
    assert claim(attempt.pk) == "refused"
    assert claim(uuid4()) == "refused"
    # The runtime role cannot release a permit or erase an acceptance itself.
    with (
        pytest.raises(DatabaseError, match="permission denied"),
        as_actor(graph, manager_of(graph)),
    ):
        IntegrationOperation.objects.filter(pk=attempt.pk).update(
            send_permit="released"
        )
    with (
        pytest.raises(IntegrityError, match="needs provider evidence"),
        as_actor(graph, manager_of(graph)),
    ):
        IntegrationOperation.objects.filter(pk=attempt.pk).update(
            provider_reference=None
        )


def test_upgraded_failed_message_stays_retryable_after_its_successor_fails(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    _dispatch_pending(graph, message_id)
    # Before attempt attribution (comms 0012) the provider's failure of the
    # sole attempt was projected onto the message and named no attempt.
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        Message.objects.filter(pk=message_id).update(provider_state="failed")
        # Comms 0016 records that base evidence as the original's release.
        cursor.execute(PERMIT_EVIDENCE_BACKFILL)

    def state() -> str:
        with as_actor(graph, manager_of(graph)):
            return services.message_states([Message.objects.get(pk=message_id)])[
                message_id
            ]

    def retry() -> None:
        with as_actor(graph, manager_of(graph)):
            services.retry_message(
                clinic_id=graph.clinic_a, message_id=message_id, command_id=uuid4()
            )

    observed = [state()]
    retry()
    observed.append(state())
    second = _dispatch_pending(graph, message_id)
    failed = world.status_body(provider_message_id=second, status="failed")
    assert world.receive(failed).accepted == 1
    observed.append(state())
    retry()
    observed.append(state())
    assert observed == ["failed", "queued", "failed", "queued"]
    with owner_context(graph.organization_a):
        assert list(
            IntegrationOperation.objects.filter(subject_id=message_id)
            .order_by("attempt_sequence")
            .values_list("attempt_sequence", "status")
        ) == [(1, "succeeded"), (2, "succeeded"), (3, "pending")]


def test_database_admits_one_successor_only_after_a_negative_outcome(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    first = _dispatch_pending(graph, message_id)

    # Direct outbox writes, without the service's locks or state check.
    def enqueue() -> None:
        with as_actor(graph, manager_of(graph)):
            enqueue_operation(
                OperationRequest(
                    channel="whatsapp",
                    provider=PROVIDER_BY_CHANNEL["whatsapp"],
                    clinic_id=graph.clinic_a,
                    subject_type=services.SUBJECT_TYPE,
                    subject_id=message_id,
                    idempotency_key=uuid4(),
                )
            )

    with pytest.raises(IntegrityError) as accepted:
        enqueue()
    assert _refusal(accepted.value) == "comms_message_attempt_open_check"
    failed = world.status_body(provider_message_id=first, status="failed")
    assert world.receive(failed).accepted == 1

    barrier = threading.Barrier(2)
    outcomes: list[str | None] = []
    failures: list[BaseException] = []

    def contend() -> None:
        try:
            barrier.wait(timeout=30)
            enqueue()
            outcomes.append("admitted")
        except IntegrityError as refused:
            outcomes.append(_refusal(refused))
        except BaseException as error:  # noqa: BLE001 - surfaced by the assertion
            failures.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not failures
    assert "admitted" in outcomes
    assert len(outcomes) == 2
    assert outcomes.count("admitted") == 1
    assert set(outcomes) - {"admitted"} <= {
        "comms_operation_attempt_sequence_uniq",
        "comms_message_attempt_open_check",
    }
    with owner_context(graph.organization_a):
        assert list(
            IntegrationOperation.objects.filter(subject_id=message_id)
            .order_by("attempt_sequence")
            .values_list("attempt_sequence", flat=True)
        ) == [1, 2]


def test_attempt_reconciliation_refuses_outsiders_and_ordered_messages(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)

    def reconcile(
        actor: UUID, operation_id: UUID | None, message: UUID = message_id
    ) -> str | None:
        try:
            with as_actor(graph, actor), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT clinic_app.comms_reconcile_attempts(%s, %s)",
                    [message, operation_id],
                )
        except DatabaseError as refused:
            diag = getattr(refused.__cause__, "diag", None)
            return getattr(diag, "constraint_name", None) or getattr(
                diag, "sqlstate", None
            )
        return None

    outsider = create_role_actor(graph, UserClinicRole.Role.FINANCE).pk
    # Unknown and foreign messages, and actors without an inbox role, look alike.
    assert reconcile(manager_of(graph), attempt.pk, uuid4()) == "42501"
    assert reconcile(outsider, attempt.pk) == "42501"
    # An ordered message never awaits reconciliation, whatever the choice.
    for choice in (attempt.pk, uuid4(), None):
        assert reconcile(manager_of(graph), choice) == (
            "comms_message_attempt_reconciliation_check"
        )
    with (
        as_actor(graph, manager_of(graph)),
        pytest.raises(InboxRefusedError, match=Reason.INVALID_STATE),
    ):
        services.reconcile_message_attempts(
            clinic_id=graph.clinic_a,
            message_id=message_id,
            current_operation_id=attempt.pk,
        )
    with owner_context(graph.organization_a):
        assert list(
            IntegrationOperation.objects.filter(subject_id=message_id).values_list(
                "attempt_sequence", flat=True
            )
        ) == [1]


def test_delivery_proof_received_before_retry_dispatch_vetoes_the_new_send(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        first = IntegrationOperation.objects.get(subject_id=message_id)
    with runtime_role():
        assert execute_operation(first.pk) == "succeeded"
    with owner_context(graph.organization_a):
        first.refresh_from_db()
    assert first.provider_reference
    assert (
        world.receive(
            world.status_body(
                provider_message_id=first.provider_reference, status="failed"
            )
        ).accepted
        == 1
    )
    with as_actor(graph, manager_of(graph)):
        services.retry_message(
            clinic_id=graph.clinic_a, message_id=message_id, command_id=uuid4()
        )
        second = IntegrationOperation.objects.get(
            subject_id=message_id, status="pending"
        )
    assert (
        world.receive(
            world.status_body(
                provider_message_id=first.provider_reference, status="delivered"
            )
        ).accepted
        == 1
    )
    with runtime_role():
        assert execute_operation(second.pk) == "cancelled"
    with owner_context(graph.organization_a):
        second.refresh_from_db()
        assert second.provider_reference is None
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "delivered"
        }
        assert set(
            InboundEvent.objects.filter(kind="status").values_list(
                "operation_id", "delivery_state"
            )
        ) == {
            (first.pk, "failed"),
            (first.pk, "delivered"),
        }


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
    url = f"/clinics/{graph.clinic_a}/messages/"
    selected = {"conversation_id": str(conversation.pk)}
    with runtime_role():
        shown = client.post(url, {"action": "show", **selected})
        form = _template_form(shown.content.decode(), template)
    assert re.findall(r'name="(p\d+)"', form) == ["p1", "p2"]
    with runtime_role():
        sent = client.post(
            url,
            {
                "action": "send",
                **selected,
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
    url = f"/clinics/{graph.clinic_a}/messages/"
    with runtime_role():
        refused = client.post(
            url,
            {
                "action": "send",
                "conversation_id": str(conversation.pk),
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
    url = f"/clinics/{graph.clinic_a}/messages/"
    with runtime_role():
        refused = client.post(
            url,
            {
                "action": "assign",
                "conversation_id": str(uuid4()),
                "assignee": "me",
                "expected_revision": "1",
            },
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


def _forge(tenant: UUID, sql: str, params: list[str], *, lock: bool = False) -> str:
    """Run one write as the runtime role (or the lock session); name its refusal."""
    alias = "locks" if lock else "default"
    try:
        with transaction.atomic(using=alias), connections[alias].cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(tenant)],
            )
            if not lock:
                cursor.execute("SET LOCAL ROLE clinic_app")
            cursor.execute(sql, params)
    except DatabaseError as refused:
        diag = getattr(refused.__cause__, "diag", None)
        return str(
            getattr(diag, "constraint_name", None) or getattr(diag, "sqlstate", None)
        )
    return "ok"


_SET = "UPDATE clinic_app.comms_integrationoperation SET {} WHERE id = %s"
_TAKE_LOCK = (
    "SELECT 1 / pg_catalog.pg_try_advisory_xact_lock(pg_catalog.hashtextextended("
    "'clinic-lock-v1:comms-subject:comms.message:' || %s, 0))::int; "
)


def _in_flight(
    graph: RbacGraph, message_id: UUID, during: Callable[[UUID], None], **raises: object
) -> tuple[str, int]:
    """Dispatch the pending attempt and run ``during`` while it is in flight."""
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(
            subject_id=message_id, status="pending"
        )
    adapter = integration._SEND_ADAPTERS[attempt.provider]
    original, calls = adapter.send, []

    def send(prepared: object, *, operation_id: UUID) -> object:
        calls.append(operation_id)
        during(operation_id)
        if "error" in raises:
            raise raises["error"]  # type: ignore[misc]
        return original(prepared, operation_id=operation_id)

    with _real_runtime(), patch.object(adapter, "send", send):
        outcome = execute_operation(attempt.pk)
    return outcome, len(calls)


@contextmanager
def _real_runtime() -> Iterator[None]:
    """The runtime role with its real role settings, as a clinic_app login has.

    ``SET ROLE`` alone skips role-level settings, so the 15 s
    idle_in_transaction_session_timeout and the rest are applied explicitly.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT unnest(setconfig) FROM pg_catalog.pg_db_role_setting"
            " WHERE setrole = 'clinic_app'::regrole AND setdatabase = 0"
        )
        applied = [str(row[0]).split("=", 1) for row in cursor.fetchall()]
    assert ["idle_in_transaction_session_timeout", "15s"] in applied
    with runtime_role():
        with connection.cursor() as cursor:
            for name, value in applied:
                cursor.execute(
                    "SELECT pg_catalog.set_config(%s, %s, false)", [name, value]
                )
        try:
            yield
        finally:
            try:
                with connection.cursor() as cursor:
                    for name, _value in applied:
                        cursor.execute(
                            "SELECT pg_catalog.set_config(%s, %s, false)",
                            [name, ""],
                        )
                    cursor.execute("RESET ALL")
            except (DatabaseError, InterfaceError):
                connection.close()


def test_a_claimed_send_is_written_only_by_its_claim(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    seen: dict[str, str] = {}

    def forge(operation_id: UUID) -> None:
        attempt = str(operation_id)
        for name, change in {
            "failed": "status = 'failed', last_error = 'provider_rejected'",
            "pending": "status = 'pending'",
            "cancelled": "status = 'cancelled'",
            "reference": "provider_reference = 'forged'",
            "permit": "send_permit = NULL",
        }.items():
            seen[name] = _forge(graph.organization_a, _SET.format(change), [attempt])
        # A further attempt is not admitted while this one is in flight.
        seen["successor"] = _admit(graph, message_id)
        record = (
            "SELECT clinic_app.comms_record_send_outcome(%s, 'rejected', NULL, NULL)"
        )
        # Another session may not record it while the claimant lives.
        seen["other_session"] = _forge(graph.organization_a, record, [attempt])

    assert _in_flight(graph, message_id, forge) == ("succeeded", 1)
    assert seen == {
        "failed": "comms_message_attempt_claim_check",
        "pending": "comms_message_attempt_claim_check",
        "cancelled": "comms_message_attempt_claim_check",
        "reference": "comms_message_attempt_claim_check",
        "permit": "42501",
        "successor": "comms_message_attempt_open_check",
        "other_session": "42501",
    }
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    assert (attempt.status, attempt.send_permit, attempt.send_permit_claim) == (
        "succeeded",
        "held",
        None,
    )
    # Once recorded, acceptance is permanent and admits no successor.
    assert _forge(
        graph.organization_a,
        _SET.format("provider_reference = NULL"),
        [str(attempt.pk)],
    ) == ("comms_message_attempt_outcome_check")
    # The runtime role holds no grant on the order; even the owner cannot
    # rewrite it.
    assert _forge(
        graph.organization_a, _SET.format("attempt_sequence = 9"), [str(attempt.pk)]
    ) == ("42501")
    with (
        pytest.raises(IntegrityError, match="attempt order is immutable"),
        owner_context(graph.organization_a),
    ):
        IntegrationOperation.objects.filter(pk=attempt.pk).update(attempt_sequence=9)
    assert _admit(graph, message_id) == "comms_message_attempt_open_check"


def _admit(graph: RbacGraph, message_id: UUID) -> str:
    try:
        with as_actor(graph, manager_of(graph)):
            enqueue_operation(
                OperationRequest(
                    channel="whatsapp",
                    provider=PROVIDER_BY_CHANNEL["whatsapp"],
                    clinic_id=graph.clinic_a,
                    subject_type=services.SUBJECT_TYPE,
                    subject_id=message_id,
                    idempotency_key=uuid4(),
                )
            )
    except IntegrityError as refused:
        return str(_refusal(refused))
    return "admitted"


def test_admission_needs_every_attempt_closed_by_its_own_outcome(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    # Queued, then accepted: both open.
    assert _admit(graph, message_id) == "comms_message_attempt_open_check"
    reference = _dispatch_pending(graph, message_id)
    assert _admit(graph, message_id) == "comms_message_attempt_open_check"
    # A delivery proof keeps it closed to successors even after a failure.
    world.receive(world.status_body(provider_message_id=reference, status="failed"))
    with owner_context(graph.organization_a):
        Message.objects.filter(pk=message_id).update(provider_state="delivered")
    assert _admit(graph, message_id) == "comms_message_attempt_open_check"
    with owner_context(graph.organization_a):
        Message.objects.filter(pk=message_id).update(provider_state="failed")
    # The provider's own failure closes it: exactly one successor.
    assert _admit(graph, message_id) == "admitted"
    assert _admit(graph, message_id) == "comms_message_attempt_open_check"


def test_synchronous_rejection_and_transient_refusal_are_recorded_by_the_claim(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    transient = _in_flight(
        graph, message_id, lambda _: None, error=TransientSendError()
    )
    assert transient == ("retry", 1)
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    assert (attempt.status, attempt.send_permit_claim) == ("pending", None)
    rejected = _in_flight(graph, message_id, lambda _: None, error=PermanentSendError())
    assert rejected == ("failed", 1)
    # A genuine rejection admits exactly one retry, which is sent.
    with as_actor(graph, manager_of(graph)):
        services.retry_message(
            clinic_id=graph.clinic_a, message_id=message_id, command_id=uuid4()
        )
    assert _admit(graph, message_id) == "comms_message_attempt_open_check"
    assert _dispatch_pending(graph, message_id)
    # The runtime role cannot insert a permit or a claim of its own.
    with owner_context(graph.organization_a):
        assert set(
            IntegrationOperation.objects.filter(subject_id=message_id).values_list(
                "send_permit_claim", flat=True
            )
        ) == {
            None
        }  # The runtime role holds no grant on the order; even the owner cannot
    # rewrite it.
    assert (
        _forge(
            graph.organization_a, _SET.format("attempt_sequence = 9"), [str(attempt.pk)]
        )
        == "42501"
    )
    with (
        pytest.raises(IntegrityError, match="attempt order is immutable"),
        owner_context(graph.organization_a),
    ):
        IntegrationOperation.objects.filter(pk=attempt.pk).update(attempt_sequence=9)


def test_a_dead_claimant_leaves_its_send_in_flight_for_any_successor(
    world: InboxWorld, superuser_database_url: str
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    seen: dict[str, str] = {}

    def take_over(operation_id: UUID) -> None:
        # Nothing secret is visible in another session's activity.
        with connections["locks"].cursor() as cursor:
            cursor.execute("SELECT pg_catalog.pg_backend_pid()")
            claimant = cursor.fetchone()[0]
        with psycopg.connect(superuser_database_url, autocommit=True) as admin:
            query = admin.execute(
                "SELECT query FROM pg_catalog.pg_stat_activity WHERE pid = %s",
                [claimant],
            ).fetchone()
            seen["visible"] = "nonce" if "claim_" in str(query) else "nothing"
            # Returns once the claimant's session is gone (or the bound passes).
            admin.execute(
                "SELECT pg_catalog.pg_terminate_backend(%s, 60000)", [claimant]
            )
        record = (
            "SELECT clinic_app.comms_record_send_outcome(%s, 'rejected', NULL, NULL)"
        )
        # The freed lock and any replay give a successor session nothing.
        seen["lock_taker"] = _forge(
            graph.organization_a,
            _TAKE_LOCK + record,
            [str(message_id), str(operation_id)],
        )
        seen["status"] = _forge(
            graph.organization_a, _SET.format("status = 'pending'"), [str(operation_id)]
        )

    try:
        with pytest.raises(DatabaseError):
            _in_flight(graph, message_id, take_over)
    finally:
        connections["locks"].close()
    assert seen == {
        "visible": "nothing",
        "lock_taker": "42501",
        "status": "comms_message_attempt_claim_check",
    }
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    # Abandoned, not failed: it stays in flight, held for staff, and sends no more.
    assert (attempt.status, attempt.provider_reference) == ("in_progress", None)
    with runtime_role():
        assert execute_operation(attempt.pk) != "succeeded"
    with as_actor(graph, manager_of(graph)):
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "reconciliation"
        }
    # A verified receipt for the attempt's own synthetic reference still
    # matches it, so the acceptance and its reference are not lost.
    reference = f"synthetic:whatsapp:{attempt.pk}"
    delivered = world.status_body(provider_message_id=reference, status="delivered")
    assert world.receive(delivered).accepted == 1
    with owner_context(graph.organization_a):
        attempt.refresh_from_db()
    assert (attempt.status, attempt.provider_reference) == ("succeeded", reference)
    with as_actor(graph, manager_of(graph)):
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "delivered"
        }


def _value(tenant: UUID, sql: str, params: list[str], *, lock: bool = False) -> object:
    """Run one statement as the runtime role (or lock session); return its value."""
    alias = "locks" if lock else "default"
    with transaction.atomic(using=alias), connections[alias].cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(tenant)],
        )
        if not lock:
            cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def test_the_runtime_role_cannot_forge_an_unclaimed_outcome(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    tenant, op = graph.organization_a, str(attempt.pk)
    accepted = "status = 'succeeded', provider_reference = 'forged'"
    assert _forge(tenant, _SET.format(accepted), [op]) == (
        "comms_message_attempt_outcome_check"
    )
    projection = "UPDATE clinic_app.comms_message SET provider_state = %s WHERE id = %s"
    assert _forge(tenant, projection, ["delivered", str(message_id)]) == "42501"
    # The provider secret is unreadable, and its read-only digest opens nothing.
    assert _forge(
        tenant, "SELECT secret FROM clinic_app.comms_providermacsecret", []
    ) == ("42501")
    with owner_context(graph.organization_a):
        digest = ProviderAccount.objects.get(pk=world.account).signing_key_digest
    resolved = "SELECT count(*) FROM clinic_app.comms_resolve_inbound(%s, %s)"
    assert _value(tenant, resolved, [str(world.account), digest]) == 0
    with as_actor(graph, manager_of(graph)):
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "queued"
        }


def test_a_squatting_claimant_cannot_record_a_forged_acceptance(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    other = _send(graph, _only_conversation(graph).pk, body="Outra").message_id
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
        foreign = IntegrationOperation.objects.get(subject_id=other)
    tenant, op = graph.organization_a, str(attempt.pk)
    claim = "SELECT clinic_app.comms_claim_send_permit(%s)"
    # The runtime role may neither claim nor record, in any state.
    record = "SELECT clinic_app.comms_record_send_outcome(%s, 'accepted', %s, %s)"
    assert _forge(tenant, claim, [op]) == "42501"
    assert _forge(tenant, record, [op, "forged", "0" * 64]) == "42501"
    # Even the lock session cannot claim a queued attempt.
    assert _value(tenant, claim, [op], lock=True) == "refused"
    # Once the outbox claimed it, a squatter holding the lock may claim, but
    # a guessed MAC, a MAC for another attempt and one for another reference
    # are all refused.
    assert _forge(tenant, _SET.format("status = 'in_progress'"), [op]) == "ok"
    squat = (
        _TAKE_LOCK
        + claim
        + "; "
        + ("SELECT clinic_app.comms_record_send_outcome(%s, 'accepted', %s, %s)")
    )
    reference = f"synthetic:whatsapp:{attempt.pk}"
    for proof in (
        "0" * 64,
        inbox_adapters.outcome_proof(
            world.account, foreign.pk, "accepted", f"synthetic:whatsapp:{foreign.pk}"
        ),
        inbox_adapters.outcome_proof(world.account, attempt.pk, "accepted", "other"),
    ):
        assert _forge(
            tenant, squat, [str(message_id), op, op, reference, proof], lock=True
        ) == ("42501")
    with owner_context(graph.organization_a):
        attempt.refresh_from_db()
    assert (attempt.status, attempt.provider_reference) == ("in_progress", None)


def test_settled_attempts_cannot_be_rewritten(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    sent = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    _dispatch_pending(graph, sent)
    queued = _send(graph, _only_conversation(graph).pk, body="Outra").message_id
    with owner_context(graph.organization_a):
        accepted = IntegrationOperation.objects.get(subject_id=sent)
        claimed = IntegrationOperation.objects.get(subject_id=queued)
    tenant = graph.organization_a
    # The outbox may claim a queued attempt directly; nothing else may move it.
    assert _forge(tenant, _SET.format("status = 'in_progress'"), [str(claimed.pk)]) == (
        "ok"
    )
    rewrites = [
        (accepted.pk, "status = 'failed'"),
        (accepted.pk, "status = 'pending'"),
        (claimed.pk, "status = 'cancelled'"),
        (claimed.pk, "status = 'failed'"),
    ]
    assert [
        _forge(tenant, _SET.format(change), [str(pk)]) for pk, change in rewrites
    ] == ["comms_message_attempt_settled_check"] * 4
    with owner_context(graph.organization_a):
        accepted.refresh_from_db()
        claimed.refresh_from_db()
    assert (accepted.status, claimed.status) == ("succeeded", "in_progress")


def test_an_abandoned_or_withdrawn_claim_never_strands_the_message(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    tenant = graph.organization_a
    claim = "SELECT clinic_app.comms_claim_send_permit(%s)"
    withdraw = (
        "; SELECT clinic_app.comms_record_send_outcome(%s, 'withdrawn', NULL, NULL)"
    )
    for leave_claim in (False, True):
        message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
        with owner_context(graph.organization_a):
            attempt = IntegrationOperation.objects.get(subject_id=message_id)
        op = str(attempt.pk)
        assert _forge(tenant, _SET.format("status = 'in_progress'"), [op]) == "ok"
        if leave_claim:
            # A lock session claims and dies: the claim is abandoned and the
            # message is held for staff, who close it; one retry then sends.
            assert (
                _forge(tenant, _TAKE_LOCK + claim, [str(message_id), op], lock=True)
                == "ok"
            )
            with as_actor(graph, manager_of(graph)):
                states = services.message_states([Message.objects.get(pk=message_id)])
                assert states == {message_id: "reconciliation"}
                services.reconcile_message_attempts(
                    clinic_id=graph.clinic_a,
                    message_id=message_id,
                    current_operation_id=None,
                )
        else:
            # A claimant's own withdrawal closes the attempt unsent.
            sql = _TAKE_LOCK + claim + withdraw
            assert _forge(tenant, sql, [str(message_id), op, op], lock=True) == "ok"
        with owner_context(graph.organization_a):
            attempt.refresh_from_db()
        assert attempt.send_permit_claim is None
        assert attempt.status in {"cancelled", "failed"}
        with as_actor(graph, manager_of(graph)):
            services.retry_message(
                clinic_id=graph.clinic_a, message_id=message_id, command_id=uuid4()
            )
        assert _dispatch_pending(graph, message_id)


def test_no_query_text_carries_the_provider_secret(
    world: InboxWorld, superuser_database_url: str
) -> None:
    """Snapshot every session's query text after each statement we run."""
    graph = world.graph
    seen: list[str] = []
    with psycopg.connect(superuser_database_url, autocommit=True) as admin:

        def snapshot(
            execute: Callable[..., object], sql: str, params: object, *rest: object
        ) -> object:
            result = execute(sql, params, *rest)
            seen.extend(
                str(row[0])
                for row in admin.execute(
                    "SELECT query FROM pg_catalog.pg_stat_activity"
                    " WHERE datname = pg_catalog.current_database()"
                ).fetchall()
            )
            return result

        with (
            connections["default"].execute_wrapper(snapshot),
            connections["locks"].execute_wrapper(snapshot),
        ):
            world.receive(world.message_body(text="Oi"))
            message_id = _send(
                graph, _only_conversation(graph).pk, body="Olá"
            ).message_id
            reference = _dispatch_pending(graph, message_id)
            world.receive(
                world.status_body(provider_message_id=reference, status="sent")
            )
    assert any("comms_resolve_inbound" in query for query in seen)
    assert any("comms_record_send_outcome" in query for query in seen)
    assert not [query for query in seen if world.key in query]


def test_no_runtime_session_can_signal_a_live_lock_session(
    world: InboxWorld, app_database_url: str
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    seen: dict[str, object] = {}

    def attack(operation_id: UUID) -> None:
        del operation_id
        with connections["locks"].cursor() as cursor:
            cursor.execute("SELECT current_user, pg_catalog.pg_backend_pid()")
            seen["role"], pid = cursor.fetchone()
        with psycopg.connect(app_database_url, autocommit=True) as app:
            for signal in ("pg_terminate_backend", "pg_cancel_backend"):
                try:
                    app.execute(f"SELECT pg_catalog.{signal}(%s)", [pid])
                    seen[signal] = "ran"
                except psycopg.errors.InsufficientPrivilege:
                    seen[signal] = "42501"
            # The claim stays live, so the staff hold never opens on it.
            row = app.execute(
                "SELECT clinic_app.comms_claim_abandoned(%s)", [message_id]
            ).fetchone()
            seen["abandoned"] = row[0] if row else None

    assert _in_flight(graph, message_id, attack) == ("succeeded", 1)
    assert seen == {
        "role": "clinic_lock",
        "pg_terminate_backend": "42501",
        "pg_cancel_backend": "42501",
        "abandoned": False,
    }


def _lock_session(superuser_database_url: str, tenant: UUID) -> psycopg.Connection:
    """Open a session as the role the lock path uses, in one open transaction."""
    session = psycopg.connect(superuser_database_url)
    role = (
        "clinic_lock"
        if session.execute(
            "SELECT pg_catalog.has_function_privilege('clinic_lock',"
            " 'clinic_app.comms_claim_send_permit(uuid)', 'EXECUTE')"
        ).fetchone()
        == (True,)
        else "clinic_app"
    )
    session.execute(f"SET ROLE {role}")
    session.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, true)", [str(tenant)]
    )
    return session


def _lock_sql(
    superuser_database_url: str, tenant: UUID, statements: list[tuple[str, list[str]]]
) -> str:
    """Run statements in one lock-path transaction; name its refusal or "ok"."""
    session = _lock_session(superuser_database_url, tenant)
    try:
        for sql, params in statements:
            session.execute(sql, params)
        session.commit()
    except psycopg.Error as refused:
        return str(refused.sqlstate)
    finally:
        session.close()
    return "ok"


def test_a_close_racing_a_claim_leaves_no_stale_claim(
    world: InboxWorld, superuser_database_url: str
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    op = str(attempt.pk)
    assert _forge(
        graph.organization_a, _SET.format("status = 'in_progress'"), [op]
    ) == ("ok")
    closer = _lock_session(superuser_database_url, graph.organization_a)
    claimant = _lock_session(superuser_database_url, graph.organization_a)
    claimed: list[str] = []
    started = threading.Barrier(2)
    try:
        # The close holds the attempt's row lock, uncommitted.
        closed = closer.execute(
            "SELECT clinic_app.comms_close_unsent_attempt(%s, 'cancelled',"
            " 'subject_ineligible')",
            [op],
        ).fetchone()
        assert closed == (True,)
        claimant.execute(_TAKE_LOCK.rstrip("; "), [str(message_id)])
        pid = claimant.info.backend_pid

        def claim() -> None:
            started.wait()
            row = claimant.execute(
                "SELECT clinic_app.comms_claim_send_permit(%s)", [op]
            ).fetchone()
            claimed.append(str(row[0]) if row else "")
            claimant.commit()

        racer = threading.Thread(target=claim)
        racer.start()
        started.wait()
        # Commit the close only once the claim's final write waits on it.
        with psycopg.connect(superuser_database_url, autocommit=True) as watch:
            blocked = (closer.info.backend_pid,)
            for _ in range(1_000_000):
                row = watch.execute(
                    "SELECT pg_catalog.pg_blocking_pids(%s)", [pid]
                ).fetchone()
                if row and tuple(row[0]) == blocked:
                    break
            else:
                pytest.fail("the claim never waited on the close")
        closer.commit()
        racer.join()
    finally:
        closer.close()
        claimant.close()
    assert claimed == ["refused"]
    with owner_context(graph.organization_a):
        attempt.refresh_from_db()
    assert (attempt.status, attempt.send_permit_claim) == ("cancelled", None)


def test_the_unsent_close_is_tenant_scoped_with_one_denial(
    world: InboxWorld, superuser_database_url: str
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    op = str(attempt.pk)
    assert _forge(
        graph.organization_a, _SET.format("status = 'in_progress'"), [op]
    ) == ("ok")
    close = "SELECT clinic_app.comms_close_unsent_attempt(%s, 'cancelled', %s)"
    # Another tenant's session, an unknown attempt and a free-form reason all
    # get the same denial; nothing moves.
    url = superuser_database_url
    assert [
        _lock_sql(url, graph.organization_b, [(close, [op, "subject_ineligible"])]),
        _lock_sql(
            url, graph.organization_a, [(close, [str(uuid4()), "subject_ineligible"])]
        ),
        _lock_sql(url, graph.organization_a, [(close, [op, "made_up_reason"])]),
    ] == ["42501"] * 3
    with owner_context(graph.organization_a):
        attempt.refresh_from_db()
    assert attempt.status == "in_progress"


def test_a_receipt_after_a_staff_close_shows_a_possible_duplicate(
    world: InboxWorld, superuser_database_url: str
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    op, tenant = str(attempt.pk), graph.organization_a
    claim = "SELECT clinic_app.comms_claim_send_permit(%s)"
    assert _forge(tenant, _SET.format("status = 'in_progress'"), [op]) == "ok"
    # The claimant sends and dies before recording; staff close it as having
    # no recorded acceptance.
    taken = _lock_sql(
        superuser_database_url,
        tenant,
        [(_TAKE_LOCK.rstrip("; "), [str(message_id)]), (claim, [op])],
    )
    assert taken == "ok"
    with as_actor(graph, manager_of(graph)):
        services.reconcile_message_attempts(
            clinic_id=graph.clinic_a, message_id=message_id, current_operation_id=None
        )
    # The provider did accept it: the receipt is kept, not dropped.
    reference = f"synthetic:whatsapp:{attempt.pk}"
    world.receive(world.status_body(provider_message_id=reference, status="delivered"))
    with owner_context(graph.organization_a):
        attempt.refresh_from_db()
        assert InboundEvent.objects.filter(operation_id=attempt.pk).exists()
    assert (attempt.status, attempt.last_error) == ("failed", "outcome_unknown")
    with as_actor(graph, manager_of(graph)):
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "duplicate"
        }


def _deadlock_on(
    statement: str, times: int, seen: list[object]
) -> Callable[..., object]:
    """Fail the first ``times`` matching lock-session statements as deadlocks."""
    left = [times]

    def wrapper(
        execute: Callable[..., object], sql: str, params: object, *rest: object
    ) -> object:
        if statement in sql:
            seen.append(params)
            if left[0]:
                left[0] -= 1
                raise OperationalError from psycopg.errors.DeadlockDetected()
        return execute(sql, params, *rest)

    return wrapper


def test_a_deadlock_while_recording_an_acceptance_is_retried_once(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    recorded: list[object] = []
    deadlock = _deadlock_on("comms_record_send_outcome", 1, recorded)
    with connections["locks"].execute_wrapper(deadlock):
        assert _in_flight(graph, message_id, lambda _op: None) == ("succeeded", 1)
    # Same claim, same session, same MAC: the retry repeats the recording.
    assert len(recorded) == 2
    assert recorded[0] == recorded[1]
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    assert attempt.status == "succeeded"
    assert attempt.provider_reference == f"synthetic:whatsapp:{attempt.pk}"
    with as_actor(graph, manager_of(graph)):
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "sent"
        }


def test_a_deadlock_at_claim_time_leaves_the_send_claimable(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    claims: list[object] = []
    deadlock = _deadlock_on("comms_claim_send_permit", 1, claims)
    with connections["locks"].execute_wrapper(deadlock):
        assert _in_flight(graph, message_id, lambda _op: None) == ("succeeded", 1)
    assert len(claims) >= 2
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    assert attempt.status == "succeeded"


_SUBJECT = "clinic-lock-v1:comms-subject:"


class _Attacker:
    """A clinic_app session that holds a row or advisory lock and re-waits on
    the claimant's subject lock in 600 ms slices, under the 1 s deadlock
    timeout, so its own waits never run the deadlock detector."""

    def __init__(  # noqa: PLR0913 - the attacker's whole shape
        self,
        app_url: str,
        tenant: UUID,
        message_id: UUID,
        *,
        row: str | None = None,
        advisory: str | None = None,
        slices: int = 60,
    ) -> None:
        self.acquired, self.release = threading.Event(), threading.Event()
        self.failures: list[BaseException] = []
        self._args = (app_url, str(tenant), f"{_SUBJECT}comms.message:{message_id}")
        self._row, self._advisory, self._slices = row, advisory, slices
        self._thread = threading.Thread(target=self._run)

    def _run(self) -> None:
        app_url, tenant, subject = self._args
        try:
            with psycopg.connect(app_url) as attacker:
                attacker.execute(
                    "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                    [tenant],
                )
                if self._row is not None:
                    attacker.execute(
                        "SELECT 1 FROM clinic_app.comms_integrationoperation"
                        " WHERE id = %s FOR UPDATE",
                        [self._row],
                    )
                if self._advisory is not None:
                    attacker.execute(
                        "SELECT pg_catalog.pg_advisory_xact_lock("
                        "pg_catalog.hashtextextended(%s, 0))",
                        [self._advisory],
                    )
                attacker.execute("SET LOCAL lock_timeout = '600ms'")
                self.acquired.set()
                # Bounded by a slice count, not a timer.
                for _slice in range(self._slices):
                    if self.release.is_set():
                        break
                    try:
                        with attacker.transaction():
                            attacker.execute(
                                "SELECT pg_catalog.pg_advisory_xact_lock("
                                "pg_catalog.hashtextextended(%s, 0))",
                                [subject],
                            )
                    except psycopg.errors.LockNotAvailable:
                        continue
                    # The claimant let go of its subject lock: nothing to hold.
                    break
                attacker.rollback()
        except BaseException as failure:  # noqa: BLE001 - reported to the test
            self.failures.append(failure)
            self.acquired.set()

    def start(self) -> None:
        self._thread.start()
        assert self.acquired.wait(timeout=30)
        assert not self.failures

    def stop(self) -> None:
        self.release.set()
        self._thread.join(timeout=30)
        assert not self._thread.is_alive()
        assert not self.failures


def _counting(
    statement: str, *, on: int, then: Callable[[], None], seen: list[object]
) -> Callable[..., object]:
    """Call ``then`` before the ``on``-th lock-session ``statement``."""

    def wrapper(
        execute: Callable[..., object], sql: str, params: object, *rest: object
    ) -> object:
        if statement in sql and (
            statement != "pg_advisory_lock" or _SUBJECT in str(params)
        ):
            seen.append(params)
            if len(seen) == on:
                then()
        return execute(sql, params, *rest)

    return wrapper


@pytest.mark.parametrize("site", ["claim", "recording", "boundary"])
def test_a_sliced_lock_attacker_only_stalls_the_send(
    world: InboxWorld, app_database_url: str, site: str
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    conversation = _only_conversation(graph)
    message_id = _send(graph, conversation.pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    boundary = f"{_SUBJECT}{services.conversation_lock_key(conversation.pk)}"
    attacker = _Attacker(
        app_database_url,
        graph.organization_a,
        message_id,
        row=None if site == "boundary" else str(attempt.pk),
        advisory=boundary if site == "boundary" else None,
    )
    seen: list[object] = []
    # Released only after four contended tries: more than any count cap.
    if site == "recording":
        watched = _counting(
            "comms_record_send_outcome", on=5, then=attacker.release.set, seen=seen
        )
        during: Callable[[UUID], None] = lambda _op: attacker.start()  # noqa: E731
    else:
        statement = "comms_claim_send_permit" if site == "claim" else "pg_advisory_lock"

        def then() -> None:
            # Claim #2 is the first taken under the subject lock; advisory
            # lock #2 is the boundary wait (#1 is the subject lock itself).
            if len(seen) == 2:
                attacker.start()
            elif len(seen) == 6:
                attacker.release.set()

        watched = _each(statement, then, seen)
        during = lambda _op: None  # noqa: E731
    try:
        with connections["locks"].execute_wrapper(watched):
            assert _in_flight(graph, message_id, during) == ("succeeded", 1)
    finally:
        attacker.stop()
    # Each wait was bounded by the lock session's lock_timeout and retried
    # while the attacker held on: more tries than any count cap allowed.
    assert len(seen) >= (5 if site == "recording" else 6)
    with owner_context(graph.organization_a):
        attempt.refresh_from_db()
    assert attempt.status == "succeeded"
    with as_actor(graph, manager_of(graph)):
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "sent"
        }


# 34 tries bounded by the 500 ms lock_timeout: a stall past the runtime
# role's 15 s idle_in_transaction_session_timeout, counted, not timed.
_LONG_STALL = 34


@pytest.mark.parametrize("site", ["claim", "recording", "boundary"])
def test_a_stall_past_the_runtime_idle_timeout_still_sends_once(
    world: InboxWorld, app_database_url: str, site: str
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    conversation = _only_conversation(graph)
    message_id = _send(graph, conversation.pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    boundary = f"{_SUBJECT}{services.conversation_lock_key(conversation.pk)}"
    attacker = _Attacker(
        app_database_url,
        graph.organization_a,
        message_id,
        row=None if site == "boundary" else str(attempt.pk),
        advisory=boundary if site == "boundary" else None,
        slices=2 * _LONG_STALL,
    )
    seen: list[object] = []
    statement = {
        "claim": "comms_claim_send_permit",
        "recording": "comms_record_send_outcome",
        "boundary": "pg_advisory_lock",
    }[site]

    def then() -> None:
        if site != "recording" and len(seen) == 2:
            attacker.start()
        elif len(seen) == _LONG_STALL:
            attacker.release.set()

    def during(_op: UUID) -> None:
        if site == "recording":
            attacker.start()

    try:
        with connections["locks"].execute_wrapper(_each(statement, then, seen)):
            assert _in_flight(graph, message_id, during) == ("succeeded", 1)
    finally:
        attacker.stop()
    assert len(seen) >= _LONG_STALL
    with owner_context(graph.organization_a):
        attempt.refresh_from_db()
    assert attempt.status == "succeeded"
    with as_actor(graph, manager_of(graph)):
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "sent"
        }


def test_a_lock_session_killed_at_claim_time_returns_the_send_to_pending(
    world: InboxWorld, app_database_url: str, superuser_database_url: str
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    attacker = _Attacker(
        app_database_url, graph.organization_a, message_id, row=str(attempt.pk)
    )
    killed: list[int] = []
    seen: list[object] = []

    def then() -> None:
        if len(seen) == 2:
            attacker.start()
        elif len(seen) == 4:
            # The lock session dies while its claim waits: no permit was
            # taken and no provider call made.
            pid = connections["locks"].connection.info.backend_pid
            killed.append(pid)
            attacker.release.set()
            with psycopg.connect(superuser_database_url, autocommit=True) as admin:
                admin.execute("SELECT pg_catalog.pg_terminate_backend(%s)", [pid])

    try:
        with connections["locks"].execute_wrapper(
            _each("comms_claim_send_permit", then, seen)
        ):
            first = _in_flight(graph, message_id, lambda _op: None)
    finally:
        attacker.stop()
    assert killed
    # Proven unsent by the database (no claim, no reference): retryable.
    assert first == ("retry", 0)
    with owner_context(graph.organization_a):
        attempt.refresh_from_db()
    assert (attempt.status, attempt.send_permit_claim) == ("pending", None)
    # The next dispatch sends exactly once.
    assert _in_flight(graph, message_id, lambda _op: None) == ("succeeded", 1)
    with as_actor(graph, manager_of(graph)):
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "sent"
        }


@pytest.mark.parametrize(
    "statement", ["comms_record_send_outcome", "comms_claim_send_permit"]
)
def test_repeated_deadlocks_never_exhaust_the_claimant(
    world: InboxWorld, statement: str
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    seen: list[object] = []
    # Five deadlocks in a row: more than any fixed retry count.
    with connections["locks"].execute_wrapper(_deadlock_on(statement, 5, seen)):
        assert _in_flight(graph, message_id, lambda _op: None) == ("succeeded", 1)
    assert len(seen) >= 6
    with as_actor(graph, manager_of(graph)):
        assert services.message_states([Message.objects.get(pk=message_id)]) == {
            message_id: "sent"
        }


def _each(
    statement: str, then: Callable[[], None], seen: list[object]
) -> Callable[..., object]:
    """Record each lock-session ``statement`` and call ``then`` before it runs."""

    def wrapper(
        execute: Callable[..., object], sql: str, params: object, *rest: object
    ) -> object:
        if statement in sql and (
            statement != "pg_advisory_lock" or _SUBJECT in str(params)
        ):
            seen.append(params)
            then()
        return execute(sql, params, *rest)

    return wrapper


def test_a_lock_session_killed_mid_retry_fails_closed_with_one_call(
    world: InboxWorld, app_database_url: str, superuser_database_url: str
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    attacker = _Attacker(
        app_database_url, graph.organization_a, message_id, row=str(attempt.pk)
    )
    calls: list[UUID] = []

    killed: list[int] = []

    def kill() -> None:
        pid = connections["locks"].connection.info.backend_pid
        killed.append(pid)
        with psycopg.connect(superuser_database_url, autocommit=True) as admin:
            admin.execute("SELECT pg_catalog.pg_terminate_backend(%s)", [pid])

    def during(operation_id: UUID) -> None:
        calls.append(operation_id)
        attacker.start()

    seen: list[object] = []
    # Killed on the fifth try: past any old retry cap, mid-retry.
    watched = _counting("comms_record_send_outcome", on=5, then=kill, seen=seen)
    try:
        with connections["locks"].execute_wrapper(watched):
            try:
                outcome = _in_flight(graph, message_id, during)[0]
            except (DatabaseError, InterfaceError):
                outcome = "raised"
    finally:
        attacker.stop()
        connections["locks"].close()
    assert killed
    assert outcome != "succeeded"
    # A later dispatch of the same attempt makes no second call.
    with runtime_role():
        assert execute_operation(attempt.pk) != "succeeded"
    assert len(calls) == 1
    with owner_context(graph.organization_a):
        attempt.refresh_from_db()
    assert attempt.status == "in_progress"
    assert attempt.provider_reference is None


def test_a_recorded_outcome_replays_as_a_no_op(world: InboxWorld) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    reference = _dispatch_pending(graph, message_id)
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
    replay = "SELECT clinic_app.comms_record_send_outcome(%s, 'rejected', NULL, NULL)"
    assert _value(graph.organization_a, replay, [str(attempt.pk)], lock=True) is False
    with owner_context(graph.organization_a):
        attempt.refresh_from_db()
    assert (attempt.status, attempt.provider_reference) == ("succeeded", reference)


def test_a_claimed_outcome_and_its_audit_event_commit_together(
    world: InboxWorld,
) -> None:
    graph = world.graph
    world.receive(world.message_body(text="Oi"))
    message_id = _send(graph, _only_conversation(graph).pk, body="Olá").message_id
    original = integration._record_integration_event

    def failing(event_type: str, *args: object, **kwargs: object) -> int:
        if event_type == "comms.operation.succeeded":
            raise DatabaseError
        return original(event_type, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(integration, "_record_integration_event", failing),
        pytest.raises(DatabaseError),
    ):
        _in_flight(graph, message_id, lambda _: None)
    with owner_context(graph.organization_a):
        attempt = IntegrationOperation.objects.get(subject_id=message_id)
        events = AuditEvent.objects.filter(
            event_type="comms.operation.succeeded", affected_record_id=str(attempt.pk)
        ).count()
    # Neither the acceptance nor its event survived the rollback.
    assert (attempt.status, attempt.provider_reference, events) == (
        "in_progress",
        None,
        0,
    )
