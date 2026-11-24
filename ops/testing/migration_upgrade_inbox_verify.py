"""Verify upgraded inbox histories with the *candidate* revision's own code.

Called by ``migration_upgrade_verify.py`` after the forward migration with the
identifiers ``migration_upgrade_inbox_seed.py`` exported. Each case reports
named failures on its own:

* ``skew``/``tie``: the original attempt is current-minus-one, the accepted
  retry current; a late authenticated failure for the original neither shows
  the message failed nor admits a third attempt, through the staff command or
  a direct outbox write.
* ``chain``: the cancelled retry is current, so the message is failed and a
  staff retry is admitted.
* ``ambiguous``: no attempt is numbered; the message shows reconciliation,
  the queued retry is cancelled by the worker without a provider send, staff
  retry and direct writes are refused, and recording the current attempt is
  audited with only the allowed payload keys.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.audit.models import AuditEvent
from apps.comms import inbox_services as services
from apps.comms.inbound_providers import sign
from apps.comms.inbox_inbound import receive_inbound, secret_name
from apps.comms.inbox_queries import conversation_view
from apps.comms.inbox_services import PROVIDER_BY_CHANNEL, InboxRefusedError
from apps.comms.models import IntegrationOperation, Message
from apps.core.integration import (
    OperationRequest,
    enqueue_operation,
    execute_operation,
)
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from ops.testing.migration_upgrade_inbox_seed import status_body

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# The original attempt and its one accepted retry, never a third.
ATTEMPTS_AFTER_RETRY = 2

AUDIT_KEYS = frozenset(
    {
        "clinic_id",
        "http_method",
        "http_status",
        "object_verb",
        "reason_code",
        "request_id",
    }
)


@contextmanager
def _runtime() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


class _World:
    def __init__(self, markers: dict[str, object], inbox: dict[str, object]) -> None:
        self.organization_id = UUID(str(markers["organization_id"]))
        self.clinic_id = UUID(str(markers["clinic_id"]))
        self.manager_id = UUID(str(inbox["manager_id"]))
        self.account_id = UUID(str(inbox["account_id"]))
        secret = Path(str(settings.CLINIC_SECRET_DIR)) / (
            f"{secret_name(self.account_id)}.secret"
        )
        self.key = bytes.fromhex(secret.read_text().strip())

    @contextmanager
    def owner(self) -> Iterator[None]:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(self.organization_id)],
            )
            yield

    def staff(self, call: Callable[[], object]) -> object:
        with _runtime(), tenant_context(self.manager_id, self.organization_id):
            return call()

    def late_failure(self, operation_id: UUID) -> int:
        with self.owner():
            reference = IntegrationOperation.objects.get(
                pk=operation_id
            ).provider_reference
        body = status_body(str(reference), "failed", "1790000099")
        with _runtime():
            receipt = receive_inbound(
                provider="whatsapp",
                account_id=self.account_id,
                headers={"X-Hub-Signature-256": sign(secret=self.key, body=body)},
                body=body,
            )
        return receipt.accepted

    def state(self, message_id: UUID) -> str:
        def read() -> str:
            message = Message.objects.get(pk=message_id)
            return services.message_states([message])[message_id]

        return str(self.staff(read))

    def retry(self, message_id: UUID) -> str:
        try:
            self.staff(
                lambda: services.retry_message(
                    clinic_id=self.clinic_id, message_id=message_id, command_id=uuid4()
                )
            )
        except InboxRefusedError as refusal:
            return str(refusal)
        return "queued"

    def direct(self, message_id: UUID) -> str:
        try:
            self.staff(
                lambda: enqueue_operation(
                    OperationRequest(
                        channel="whatsapp",
                        provider=PROVIDER_BY_CHANNEL["whatsapp"],
                        clinic_id=self.clinic_id,
                        subject_type=services.SUBJECT_TYPE,
                        subject_id=message_id,
                        idempotency_key=uuid4(),
                    )
                )
            )
        except IntegrityError as refused:
            diag = getattr(refused.__cause__, "diag", None)
            return str(getattr(diag, "constraint_name", None))
        return "admitted"

    def attempts(self, message_id: UUID) -> dict[str, tuple[int | None, str, bool]]:
        with self.owner():
            rows = IntegrationOperation.objects.filter(
                subject_id=message_id
            ).values_list("pk", "attempt_sequence", "status", "provider_reference")
            return {
                str(pk): (number, status, bool(reference))
                for pk, number, status, reference in rows
            }


def _accepted_retry(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message, first, second = UUID(ids["message"]), ids["a"], ids["b"]
    numbered = world.attempts(message)
    if (numbered[first][0], numbered[second][0]) != (1, 2):
        failures.append(f"order {numbered[first][0]}/{numbered[second][0]} not 1/2")
    if world.state(message) != "sent":
        failures.append("accepted retry not shown sent after the upgrade")
    if world.late_failure(UUID(first)) != 1:
        failures.append("late authenticated failure for the original not accepted")
    if world.state(message) != "sent":
        failures.append("late failure for the original changed the shown state")
    if world.retry(message) != services.Reason.NOT_FAILED:
        failures.append("staff retry admitted while the accepted retry is current")
    if world.direct(message) != "comms_message_attempt_open_check":
        failures.append("direct outbox write admitted a third attempt")
    references = sum(accepted for _, _, accepted in world.attempts(message).values())
    total = len(world.attempts(message))
    if references != ATTEMPTS_AFTER_RETRY or total != ATTEMPTS_AFTER_RETRY:
        failures.append(
            f"{references} provider references over {total} attempts, not 2"
        )


def _chain(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    numbered = world.attempts(message)
    if (numbered[ids["a"]][0], numbered[ids["b"]][0]) != (1, 2):
        failures.append("cancelled retry is not ordered after the original")
    if world.state(message) != "failed":
        failures.append("current cancelled retry not shown failed")
    if world.retry(message) != "queued":
        failures.append("staff retry refused after the current attempt failed")


def _ambiguous(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    if any(number is not None for number, _, _ in world.attempts(message).values()):
        failures.append("unordered retries were numbered")
    if world.state(message) != "reconciliation":
        failures.append("unordered message not shown for reconciliation")
    with _runtime():
        execute_operation(UUID(ids["c"]))
    queued = world.attempts(message)[ids["c"]]
    if queued[1] != "cancelled" or queued[2]:
        failures.append("queued retry was sent while the order is unknown")
    if world.retry(message) != services.Reason.NOT_FAILED:
        failures.append("staff retry admitted while awaiting reconciliation")
    if world.direct(message) != "comms_message_attempt_reconciliation_check":
        failures.append("direct outbox write admitted while awaiting reconciliation")
    view = world.staff(
        lambda: conversation_view(
            clinic_id=world.clinic_id,
            conversation_id=UUID(ids["conversation"]),
            now=timezone.now(),
        )
    )
    rows = [row for row in getattr(view, "messages", ()) if row.message.pk == message]
    if [(row.state, len(row.attempts)) for row in rows] != [("reconciliation", 3)]:
        failures.append("staff view does not offer the attempts for reconciliation")
    world.staff(
        lambda: services.reconcile_message_attempts(
            clinic_id=world.clinic_id,
            message_id=message,
            current_operation_id=UUID(ids["b"]),
        )
    )
    numbered = world.attempts(message)
    if numbered[ids["b"]][0] != len(numbered) or None in {
        number for number, _, _ in numbered.values()
    }:
        failures.append("recorded current attempt is not the highest")
    if world.state(message) == "reconciliation":
        failures.append("message still awaits reconciliation after staff decision")
    with world.owner():
        payloads = list(
            AuditEvent.objects.filter(
                event_type="comms.message.reconciled", affected_record_id=str(message)
            ).values_list("payload", flat=True)
        )
    if len(payloads) != 1 or not set(payloads[0]) <= AUDIT_KEYS:
        failures.append("reconciliation not audited once with allowed keys")


_CASES: dict[str, Callable[[_World, dict[str, str], list[str]], None]] = {
    "skew": _accepted_retry,
    "tie": _accepted_retry,
    "chain": _chain,
    "ambiguous": _ambiguous,
}


def verify_inbox(markers: dict[str, object]) -> list[str]:
    """Return named failures for every seeded inbox history; empty when none."""
    inbox = markers.get("inbox")
    if not isinstance(inbox, dict):
        return []
    world = _World(markers, inbox)
    failures: list[str] = []
    with override_settings(COMMS_SYNTHETIC_CHANNELS=("email", "sms", "whatsapp")):
        for case, ids in inbox["cases"].items():
            found: list[str] = []
            try:
                _CASES[case](world, ids, found)
            except Exception as error:  # noqa: BLE001 - reported as a named failure
                found.append(type(error).__name__)
            failures.extend(f"inbox {case}: {name}" for name in found)
    return failures
