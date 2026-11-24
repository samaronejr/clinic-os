"""Verify upgraded inbox histories with the *candidate* revision's own code.

Called by ``migration_upgrade_verify.py`` after the forward migration with the
identifiers ``migration_upgrade_inbox_seed.py`` exported. Every case reports
named failures on its own:

* parity (``p_*``, ``r_*``, ``e_failed``, ``e_retry_sent``): the candidate
  shows exactly the state the base showed staff, and admits a staff retry
  exactly when the base offered one;
* ``skew``/``tie``: the accepted retry is current; a late failure for the
  original neither changes the state nor admits a third attempt;
* held (``ambiguous``, ``e_retry_failed``, ``none_accepted`` and the round-4
  histories): the base recorded no order, so the candidate shows
  ``reconciliation`` and sends nothing; staff may name only an attempt the
  provider accepted, or record that none was, and every queued attempt is
  cancelled in the decision's transaction;
* ``race``: a worker that claimed the queued attempt is released only after
  staff try to resolve; the decision is refused while it holds the claim, and
  no third provider reference ever appears.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event, Thread
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import UUID, uuid4

from apps.audit.models import AuditEvent
from apps.comms import inbox_services as services
from apps.comms.inbound_providers import sign
from apps.comms.inbox_inbound import receive_inbound, secret_name
from apps.comms.inbox_queries import conversation_view
from apps.comms.inbox_services import PROVIDER_BY_CHANNEL, InboxRefusedError
from apps.comms.models import IntegrationOperation, Message
from apps.core import integration
from apps.core.integration import (
    OperationRequest,
    enqueue_operation,
    execute_operation,
)
from apps.tenancy.db import tenant_context
from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from ops.testing.migration_upgrade_inbox_seed import status_body

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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
REFUSED = str(services.Reason.NOT_FAILED)
INVALID = str(services.Reason.INVALID_STATE)
PARITY = (
    "p_queued",
    "p_sent",
    "p_sent_receipt",
    "p_delivered",
    "p_read",
    "p_failed",
    "p_failed_delivered",
    "p_cancelled",
    "r_queued",
    "r_sent",
    "r_failed",
    "r_delivered",
    "r_cancelled",
    "e_failed",
    "e_retry_sent",
    "e_delivered",
    "e_unknown",
    "s_sent",
    "s_delivered",
    "s_failed",
    "s_retry_sent",
    "s_rejected",
    "o_rejected",
    "o_unknown",
    "o_rejected_retry_sent",
)
# Bounded waits for the two-thread race; the threads never sleep.
RACE_TIMEOUT = 60


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
        accounts = inbox["accounts"]
        if not isinstance(accounts, dict):
            message = "seed markers name no inbox accounts"
            raise TypeError(message)
        self.accounts = {channel: UUID(str(pk)) for channel, pk in accounts.items()}

    def key(self, channel: str) -> bytes:
        secret = Path(str(settings.CLINIC_SECRET_DIR)) / (
            f"{secret_name(self.accounts[channel])}.secret"
        )
        return bytes.fromhex(secret.read_text().strip())

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

    def late_failure(self, channel: str, operation_id: UUID) -> int:
        with self.owner():
            reference = IntegrationOperation.objects.get(
                pk=operation_id
            ).provider_reference
        body = status_body(channel, str(reference), "failed", "1790000099")
        with _runtime():
            receipt = receive_inbound(
                provider=channel,
                account_id=self.accounts[channel],
                headers={
                    "X-Hub-Signature-256": sign(secret=self.key(channel), body=body)
                },
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

    def reconcile(self, message_id: UUID, current: UUID | None) -> str:
        try:
            self.staff(
                lambda: services.reconcile_message_attempts(
                    clinic_id=self.clinic_id,
                    message_id=message_id,
                    current_operation_id=current,
                )
            )
        except InboxRefusedError as refusal:
            return str(refusal)
        return "recorded"

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

    def dispatch(self, operation_id: UUID) -> None:
        with _runtime():
            execute_operation(operation_id)

    def attempts(self, message_id: UUID) -> dict[str, tuple[int | None, str, bool]]:
        with self.owner():
            rows = IntegrationOperation.objects.filter(
                subject_id=message_id
            ).values_list("pk", "attempt_sequence", "status", "provider_reference")
            return {
                str(pk): (number, status, bool(reference))
                for pk, number, status, reference in rows
            }

    def references(self, message_id: UUID) -> int:
        return sum(accepted for _, _, accepted in self.attempts(message_id).values())

    def offered(self, conversation_id: UUID, message_id: UUID) -> set[str]:
        view = self.staff(
            lambda: conversation_view(
                clinic_id=self.clinic_id,
                conversation_id=conversation_id,
                now=timezone.now(),
            )
        )
        rows = [r for r in getattr(view, "messages", ()) if r.message.pk == message_id]
        return {str(op.pk) for row in rows for op in row.attempts}

    def pending(self, message_id: UUID) -> list[str]:
        return [
            pk
            for pk, (_, status, _) in self.attempts(message_id).items()
            if status == "pending"
        ]


def _parity(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    base = ids["base_state"]
    shown = world.state(message)
    if shown != base:
        failures.append(f"shows {shown}, the base showed {base}")
    expected = "queued" if base == "failed" else REFUSED
    if world.retry(message) != expected:
        failures.append(f"retry is not {expected} as the base offered")


def _accepted_retry(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    numbered = world.attempts(message)
    if (numbered[ids["a"]][0], numbered[ids["b"]][0]) != (1, 2):
        failures.append("the accepted retry is not current")
    if world.late_failure("whatsapp", UUID(ids["a"])) != 1:
        failures.append("late authenticated failure for the original not accepted")
    if world.state(message) != "sent":
        failures.append("late failure for the original changed the shown state")
    if world.retry(message) != REFUSED:
        failures.append("staff retry admitted while the accepted retry is current")
    if world.direct(message) != "comms_message_attempt_open_check":
        failures.append("direct outbox write admitted a third attempt")
    if world.references(message) != len(numbered):
        failures.append("an attempt holds no reference or a third one appeared")


def _held(world: _World, message: UUID, failures: list[str]) -> None:
    if any(number is not None for number, _, _ in world.attempts(message).values()):
        failures.append("an unordered history was numbered")
    if world.state(message) != "reconciliation":
        failures.append("an unordered history is not held for reconciliation")
    if world.retry(message) != REFUSED:
        failures.append("staff retry admitted while held")
    if world.direct(message) != "comms_message_attempt_reconciliation_check":
        failures.append("direct outbox write admitted while held")


def _accepted_then_queued(
    world: _World, ids: dict[str, str], failures: list[str]
) -> None:
    message = UUID(ids["message"])
    _held(world, message, failures)
    # A's failures are bound, so only the open B is a valid choice.
    if world.offered(UUID(ids["conversation"]), message) != {ids["b"]}:
        failures.append("the page offers a choice the database refuses")
    if world.reconcile(message, UUID(ids["b"])) != "recorded":
        failures.append("staff could not record the accepted attempt")
    if world.pending(message):
        failures.append("a queued attempt survived the decision")
    world.dispatch(UUID(ids["c"]))
    if world.references(message) != 2:  # noqa: PLR2004 - A and B only
        failures.append("the queued attempt reached the provider after the decision")
    if world.state(message) != "sent" or world.retry(message) != REFUSED:
        failures.append("the accepted attempt is not current and open")
    with world.owner():
        payloads = list(
            AuditEvent.objects.filter(
                event_type="comms.message.reconciled", affected_record_id=str(message)
            ).values_list("payload", flat=True)
        )
    if len(payloads) != 1 or not set(payloads[0]) <= AUDIT_KEYS:
        failures.append("the decision is not audited once with allowed keys")


def _queued_cancelled_first(
    world: _World, ids: dict[str, str], failures: list[str]
) -> None:
    message = UUID(ids["message"])
    world.dispatch(UUID(ids["c"]))
    if world.attempts(message)[ids["c"]][1:] != ("cancelled", False):
        failures.append("the held worker sent instead of cancelling")
    failures.extend(
        f"staff could record {refused or 'none'} despite an acceptance"
        for refused in (UUID(ids["c"]), None)
        if world.reconcile(message, refused) != INVALID
    )
    if world.reconcile(message, UUID(ids["b"])) != "recorded":
        failures.append("staff could not record the accepted attempt")
    if world.state(message) != "sent" or world.retry(message) != REFUSED:
        failures.append("a retry was admitted while the accepted attempt is open")
    if world.references(message) != 2:  # noqa: PLR2004 - A and B only
        failures.append("a third provider reference appeared")


def _run_race(
    world: _World, message: UUID, queued: UUID, current: UUID
) -> tuple[list[str], list[str]]:
    """Release a worker holding ``queued``'s claim only after staff decide."""
    start, claimed, release = Barrier(2), Event(), Event()
    decisions: list[str] = []
    errors: list[str] = []
    original = integration._send_claimed  # noqa: SLF001 - the worker's send seam

    def held_send(*args: object, **kwargs: object) -> object:
        # The claim is committed: staff decide while the worker holds it.
        claimed.set()
        if not release.wait(RACE_TIMEOUT):
            errors.append("worker was never released")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    def worker() -> None:
        try:
            start.wait(RACE_TIMEOUT)
            world.dispatch(queued)
        except Exception as error:  # noqa: BLE001 - reported as a named failure
            errors.append(type(error).__name__)
        finally:
            connection.close()

    def decide() -> None:
        try:
            start.wait(RACE_TIMEOUT)
            if claimed.wait(RACE_TIMEOUT):
                decisions.append(world.reconcile(message, current))
        except Exception as error:  # noqa: BLE001 - reported as a named failure
            errors.append(type(error).__name__)
        finally:
            release.set()
            connection.close()

    with patch.object(integration, "_send_claimed", held_send):
        threads = [Thread(target=worker), Thread(target=decide)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(RACE_TIMEOUT)
    return decisions, errors


def _race(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message, queued = UUID(ids["message"]), UUID(ids["c"])
    decisions, errors = _run_race(world, message, queued, UUID(ids["b"]))
    failures.extend(errors)
    # A claim on a held message never holds a permit, so it is provably
    # unsent: the decision cancels it and the released worker sends nothing.
    if decisions != ["recorded"]:
        failures.append(f"decision during the claim was {decisions}")
    if world.attempts(message)[str(queued)][1:] != ("cancelled", False):
        failures.append("the claimed queued attempt reached the provider")
    if world.state(message) != "sent" or world.retry(message) != REFUSED:
        failures.append("the accepted attempt is not current and open")
    if world.references(message) != 2:  # noqa: PLR2004 - A and B only
        failures.append("a third provider reference appeared")


def _two_failed(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    _held(world, message, failures)
    if world.reconcile(message, UUID(ids["b"])) != "recorded":
        failures.append("staff could not record the accepted attempt")
    if world.pending(message):
        failures.append("a queued attempt survived the decision")
    if world.state(message) != "failed" or world.retry(message) != "queued":
        failures.append("the current failed attempt did not admit one retry")
    if world.retry(message) != REFUSED:
        failures.append("a second new attempt was admitted")
    successor = world.pending(message)
    if len(successor) != 1:
        failures.append("the admitted retry is not the only queued attempt")
    else:
        world.dispatch(UUID(successor[0]))
        if not world.attempts(message)[successor[0]][2]:
            failures.append("the admitted retry did not reach the provider")


def _projection_failed(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    _held(world, message, failures)
    # The superseded original is released; the unattributed failure is B's
    # own only by inference, so B stays the open choice.
    if world.offered(UUID(ids["conversation"]), message) != {ids["b"]}:
        failures.append("the page offers a choice the database refuses")
    if world.reconcile(message, UUID(ids["b"])) != "recorded":
        failures.append("staff could not record the accepted attempt")
    if world.retry(message) != REFUSED or world.references(message) != 2:  # noqa: PLR2004
        failures.append("an unattributed failure reopened a send")


def _none_accepted(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    _held(world, message, failures)
    if world.offered(UUID(ids["conversation"]), message):
        failures.append("the page offers an attempt the provider never accepted")
    if world.reconcile(message, UUID(ids["c"])) != INVALID:
        failures.append("staff could name an unaccepted attempt as current")
    if world.reconcile(message, None) != "recorded":
        failures.append("staff could not record that none was accepted")
    if world.pending(message):
        failures.append("a queued attempt survived the decision")
    if world.retry(message) != "queued" or world.retry(message) != REFUSED:
        failures.append("none-accepted did not permit exactly one new send")


def _r5_a(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    _held(world, message, failures)
    if world.reconcile(message, UUID(ids["a"])) != INVALID:
        failures.append("staff could name the failed A while B is still open")
    if world.reconcile(message, UUID(ids["b"])) != "recorded":
        failures.append("staff could not record the open B")
    if world.retry(message) != REFUSED:
        failures.append("a retry was admitted while B is open")
    if world.direct(message) != "comms_message_attempt_open_check":
        failures.append("a direct outbox write was admitted while B is open")
    world.dispatch(UUID(ids["c"]))
    if world.references(message) != 2:  # noqa: PLR2004 - A and B only
        failures.append("a further attempt reached the provider")


def _r5_b(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    _held(world, message, failures)
    if world.offered(UUID(ids["conversation"]), message) != {ids["c"]}:
        failures.append("the page offers a choice the database refuses")
    if world.reconcile(message, UUID(ids["b"])) != INVALID:
        failures.append("staff could name the failed B while C is still open")
    if world.reconcile(message, UUID(ids["c"])) != "recorded":
        failures.append("staff could not record the open C")
    if world.state(message) != "sent" or world.retry(message) != REFUSED:
        failures.append("a retry was admitted while C is open")
    if world.late_failure("whatsapp", UUID(ids["c"])) != 1:
        failures.append("C's own failure was not accepted")
    _one_more_send(world, message, failures, before=3)


def _one_more_send(
    world: _World, message: UUID, failures: list[str], *, before: int
) -> None:
    """Check that a failed current attempt admits one retry, recorded as sent."""
    if world.state(message) != "failed" or world.retry(message) != "queued":
        failures.append("the failed current attempt did not admit a retry")
    if world.retry(message) != REFUSED:
        failures.append("a second retry was admitted")
    for pending in world.pending(message):
        world.dispatch(UUID(pending))
    if world.references(message) != before + 1 or world.state(message) != "sent":
        failures.append("the admitted retry was not recorded as sent")


def _stuck(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    if world.state(message) != "sent":
        failures.append("the base-retried message does not show sent")
    if world.late_failure(ids["channel"], UUID(ids["b"])) != 1:
        failures.append("B's own failure was not accepted")
    _one_more_send(world, message, failures, before=2)


def _two_dispatch(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    world.late_failure("email", UUID(ids["b"]))
    if world.retry(message) != "queued":
        failures.append("the failed retry did not admit one more")
        return
    queued = UUID(world.pending(message)[0])
    with world.owner():
        provider = IntegrationOperation.objects.get(pk=queued).provider
    adapter = integration._SEND_ADAPTERS[provider]  # noqa: SLF001
    original, start, calls = adapter.send, Barrier(2), []
    errors: list[str] = []

    def counted(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    def worker() -> None:
        try:
            start.wait(RACE_TIMEOUT)
            world.dispatch(queued)
        except Exception as error:  # noqa: BLE001 - reported as a named failure
            errors.append(type(error).__name__)
        finally:
            connection.close()

    with patch.object(adapter, "send", counted):
        threads = [Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(RACE_TIMEOUT)
    failures.extend(errors)
    if len(calls) != 1:
        failures.append(f"{len(calls)} provider calls for one permit")
    if world.attempts(message)[str(queued)][1:] != ("succeeded", True):
        failures.append("the one send was not recorded")
    if world.state(message) != "sent" or world.references(message) != 3:  # noqa: PLR2004
        failures.append("the message does not show the recorded send")


_CHILD = """
import os
import sys
from unittest.mock import patch
from uuid import UUID
from django.db import connection
from django.test import override_settings
from apps.core import integration
def hold(*args, **kwargs):
    print("claimed", flush=True)
    sys.stdin.read()
with connection.cursor() as cursor:
    cursor.execute("SET ROLE clinic_app")
with override_settings(COMMS_SYNTHETIC_CHANNELS=("email", "sms", "whatsapp")):
    with patch.object(integration, "_send_claimed", hold):
        integration.execute_operation(UUID(os.environ["CRASH_OPERATION"]))
"""


def _crash(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    """Kill a worker holding C's claim; the message must stay resolvable."""
    message, queued = UUID(ids["message"]), UUID(ids["c"])
    child = subprocess.Popen(  # noqa: S603 - this interpreter and manage.py
        [sys.executable, "manage.py", "shell", "--no-imports", "-c", _CHILD],
        env={**os.environ, "CRASH_OPERATION": str(queued)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    stdout = child.stdout
    if stdout is None:
        detail = "the worker child has no stdout"
        raise RuntimeError(detail)
    try:
        ready, _, _ = select.select([stdout], [], [], RACE_TIMEOUT)
        claimed = bool(ready) and stdout.readline().strip() == "claimed"
    finally:
        child.kill()
        child.wait(RACE_TIMEOUT)
    if not claimed or world.attempts(message)[str(queued)][1] != "in_progress":
        failures.append("the worker died without holding the claim")
    if world.reconcile(message, UUID(ids["b"])) != "recorded":
        failures.append("the dead worker's claim froze the message")
    world.dispatch(queued)
    if world.attempts(message)[str(queued)][1:] != ("cancelled", False):
        failures.append("the dead worker's attempt was not cancelled unsent")
    if world.references(message) != 2:  # noqa: PLR2004 - A and B only
        failures.append("a duplicate send occurred after the crash")


def _r6_two_open(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message = UUID(ids["message"])
    _held(world, message, failures)
    if world.offered(UUID(ids["conversation"]), message) != {ids["b"], ids["c"]}:
        failures.append("the page does not offer both open accepted attempts")
    if world.reconcile(message, UUID(ids["c"])) != "recorded":
        failures.append("staff could not record C as current")
    world.late_failure("whatsapp", UUID(ids["c"]))
    if world.state(message) != "awaiting":
        failures.append("the message does not show it awaits B's provider outcome")
    if world.retry(message) != REFUSED:
        failures.append("a retry was admitted while B has no provider outcome")
    if world.direct(message) != "comms_message_attempt_open_check":
        failures.append("a direct outbox write was admitted while B is open")
    world.late_failure("whatsapp", UUID(ids["b"]))
    _one_more_send(world, message, failures, before=3)


def _raw(world: _World, sql: str, params: list[str], *, keep: bool) -> str:
    """Run one statement as the runtime role; report its refusal, if any."""
    try:
        with world.owner(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_app")
            cursor.execute(sql, params)
            transaction.set_rollback(not keep)
    except DatabaseError as refused:
        diag = getattr(refused.__cause__, "diag", None)
        return str(
            getattr(diag, "constraint_name", None) or getattr(diag, "sqlstate", None)
        )
    return "ok"


def _r6_raw_sql(world: _World, ids: dict[str, str], failures: list[str]) -> None:
    message, b = UUID(ids["message"]), ids["b"]
    if world.reconcile(message, UUID(ids["c"])) != "recorded":
        failures.append("staff could not record C as current")
    world.late_failure("whatsapp", UUID(ids["c"]))
    update = "UPDATE clinic_app.comms_integrationoperation SET {} WHERE id = %s"
    if _raw(world, update.format("send_permit = 'released'"), [b], keep=False) != (
        "42501"
    ):
        failures.append("the runtime role could release B's permit")
    if _raw(world, update.format("provider_reference = NULL"), [b], keep=False) != (
        "comms_message_attempt_outcome_check"
    ):
        failures.append("the runtime role could erase B's acceptance")
    forged = "status = 'failed', last_error = 'provider_rejected'"
    if _raw(world, update.format(forged), [b], keep=True) != "ok":
        failures.append("the forged status write did not run")
    if world.retry(message) != REFUSED or world.state(message) != "awaiting":
        failures.append("a forged status unlocked a second send")
    if world.direct(message) != "comms_message_attempt_open_check":
        failures.append("a forged status admitted a direct outbox write")


_CASES: dict[str, Callable[[_World, dict[str, str], list[str]], None]] = {
    **dict.fromkeys(PARITY, _parity),
    "skew": _accepted_retry,
    "tie": _accepted_retry,
    "ambiguous": _two_failed,
    "e_retry_failed": _projection_failed,
    "none_accepted": _none_accepted,
    "accepted_then_queued": _accepted_then_queued,
    "queued_cancelled_first": _queued_cancelled_first,
    "race": _race,
    "r5_a": _r5_a,
    "r5_b": _r5_b,
    "e_stuck": _stuck,
    "s_stuck": _stuck,
    "e_two_dispatch": _two_dispatch,
    "crash": _crash,
    "r6_two_open": _r6_two_open,
    "r6_raw_sql": _r6_raw_sql,
    "s_retry_failed": _projection_failed,
}


def verify_inbox(markers: dict[str, object]) -> list[str]:
    """Return named failures for every seeded inbox history; empty when none."""
    inbox = markers.get("inbox")
    if not isinstance(inbox, dict):
        return []
    world = _World(markers, inbox)
    failures: list[str] = []
    cases = inbox["cases"]
    if set(cases) != set(_CASES):
        failures.append(f"inbox cases {sorted(set(cases) ^ set(_CASES))} unmatched")
    with override_settings(COMMS_SYNTHETIC_CHANNELS=("email", "sms", "whatsapp")):
        for case, ids in cases.items():
            found: list[str] = []
            try:
                _CASES[case](world, ids, found)
            except Exception as error:  # noqa: BLE001 - reported as a named failure
                found.append(type(error).__name__)
            failures.extend(f"inbox {case}: {name}" for name in found)
    return failures
