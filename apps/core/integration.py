"""Shared job and callback transaction boundary for external integrations.

Contract:
- ``enqueue_operation`` records one tenant-scoped operation inside the
  caller's tenant transaction and dispatches the job only after commit, so
  rolled-back work never reaches a worker.
- ``execute_operation`` resolves tenant scope from the stored operation,
  holds the operation's session advisory lock on a dedicated connection
  for the whole send boundary, re-enters ``tenant_context`` (which
  rechecks actor authority), verifies the stored actor still holds a
  membership in the operation's clinic, claims the row, prepares
  provider input inside the tenant transaction and performs the
  external send with no open transaction. A duplicate delivery that
  cannot take the lock reconciles without mutating, so it can never
  exhaust or terminally fail an in-flight send.
- The send boundary also holds a session advisory lock on the
  operation's subject from the final eligibility recheck until the
  provider answers, while subject mutations hold the matching
  transaction-scoped lock through ``hold_subject_mutation_lock`` until
  they commit. A committed destination or preference change therefore
  either lands before the recheck and cancels the send, or waits for
  the send to finish: a committed revocation can never be overtaken by
  an in-flight send.
- Subject types may also register a stable mutation-boundary lock key
  through ``register_subject_recheck``. The send boundary takes that
  session lock after the recheck and re-decides under it before the
  external call, while mutations hold the matching transaction lock
  through ``hold_subject_mutation_key`` until they commit. Because the
  key names a stable parent rather than the subject row, a subject
  created after a mutation enumerated existing rows still serializes
  with that mutation and with the send.
- ``receive_provider_callback`` authenticates the raw callback first, then
  resolves the stored operation and tenant through the unique stored
  ``(provider, provider_reference)`` pair; tenant claims inside external
  payloads are never read.
- Only minimal delivery references persist; credentials, message bodies and
  provider payloads stay out of the database and out of ``last_error``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

from django.db import DatabaseError, connection, connections, transaction
from django.utils import timezone

from apps.audit.events import build_integration_audit_event
from apps.audit.services import record_event
from apps.comms.adapters import (
    CallbackAuthenticationError,
    CallbackAuthenticator,
    OperationScope,
    PermanentSendError,
    SendAdapter,
    TransientSendError,
)
from apps.comms.models import IntegrationOperation
from apps.identity.models import UserClinicRole
from apps.tenancy.db import (
    TenantAccessDeniedError,
    clear_connection_tenant_gucs,
    tenant_context,
)

type ExecutionResult = Literal[
    "missing",
    "skipped",
    "reconcile",
    "retry",
    "succeeded",
    "failed",
    "cancelled",
]
type CallbackResult = Literal["applied", "duplicate", "rejected"]

DEFAULT_MAX_ATTEMPTS: Final = 5
MAX_ATTEMPTS_BOUND: Final = 32767
MAX_PROVIDER_LENGTH: Final = 64
MAX_SUBJECT_TYPE_LENGTH: Final = 128
MAX_PROVIDER_REFERENCE_LENGTH: Final = 255
_PENDING = IntegrationOperation.Status.PENDING
_IN_PROGRESS = IntegrationOperation.Status.IN_PROGRESS
_SUCCEEDED = IntegrationOperation.Status.SUCCEEDED
_DELIVERED = IntegrationOperation.Status.DELIVERED
_FAILED = IntegrationOperation.Status.FAILED
_CANCELLED = IntegrationOperation.Status.CANCELLED
_CLAIMABLE_STATUSES: Final = (_PENDING, _IN_PROGRESS)
_CLAIM_LOCK_NAMESPACE: Final = "clinic-lock-v1:comms-operation:"
_SUBJECT_LOCK_NAMESPACE: Final = "clinic-lock-v1:comms-subject:"

type _PresendDecision = Literal["send", "missing", "ineligible", "revoked"]

_SEND_ADAPTERS: dict[str, SendAdapter] = {}
_CALLBACK_AUTHENTICATORS: dict[str, CallbackAuthenticator] = {}
_SUBJECT_RECHECKS: dict[str, Callable[[OperationScope], bool]] = {}
_SUBJECT_LOCK_KEYS: dict[str, Callable[[OperationScope, UUID], str | None]] = {}


class IntegrationContextError(RuntimeError):
    """Reject boundary work without trusted tenant GUCs."""

    def __init__(self) -> None:
        """Expose one stable non-identifying message."""
        super().__init__("integration boundary requires tenant context")


class IntegrationInputError(ValueError):
    """Reject operation input outside the fixed boundary contract."""

    def __init__(self) -> None:
        """Expose one stable non-identifying message."""
        super().__init__("integration operation input is invalid")


@dataclass(frozen=True, slots=True)
class OperationRequest:
    """Caller-supplied fields for one enqueued integration operation."""

    channel: str
    provider: str
    clinic_id: UUID
    subject_type: str
    subject_id: UUID
    idempotency_key: UUID
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


class _ClinicAuthorityRevokedError(RuntimeError):
    """Signal that the stored actor lost clinic authority mid-execution."""


class _SubjectIneligibleError(RuntimeError):
    """Signal that the subject lost send eligibility mid-execution."""


@dataclass(frozen=True, slots=True)
class _ClaimDecision:
    outcome: Literal["send", "reconcile", "exhausted", "revoked", "skipped"]
    provider: str


def register_send_adapter(adapter: SendAdapter) -> None:
    """Register the provider send adapter used at execution time."""
    _SEND_ADAPTERS[adapter.provider] = adapter


def register_callback_authenticator(authenticator: CallbackAuthenticator) -> None:
    """Register the authenticator for one provider's callbacks."""
    _CALLBACK_AUTHENTICATORS[authenticator.provider] = authenticator


def hold_subject_mutation_lock(subject_type: str, subject_id: UUID) -> None:
    """Hold the subject's send-boundary lock until the caller commits.

    Subject mutations call this inside their own transaction before
    committing so the change serializes with any send boundary that is
    rechecking or delivering for the same subject.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended(%s, 0))",
            [f"{_SUBJECT_LOCK_NAMESPACE}{subject_type}:{subject_id}"],
        )


def hold_subject_mutation_key(lock_key: str) -> None:
    """Hold a stable mutation-boundary lock until the caller commits.

    ``lock_key`` is the same value the subject type's registered
    resolver returns for the send boundary. Unlike the per-subject
    lock, the key names a stable parent (for example a patient channel),
    so a subject row created while a mutation is in flight still
    serializes with that mutation and with the send boundary.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended(%s, 0))",
            [f"{_SUBJECT_LOCK_NAMESPACE}{lock_key}"],
        )


def register_subject_recheck(
    subject_type: str,
    recheck: Callable[[OperationScope], bool],
    *,
    lock_key: Callable[[OperationScope, UUID], str | None] | None = None,
) -> None:
    """Register the send-time eligibility recheck for one subject type.

    The recheck runs inside the tenant transaction immediately before the
    provider input is prepared; returning ``False`` cancels the operation
    without any external side effect. ``lock_key`` optionally resolves
    the subject's stable mutation-boundary lock key; when registered,
    the send boundary holds that session lock from its second recheck
    until the provider answers, and mutations hold the matching
    transaction lock through ``hold_subject_mutation_key``.
    """
    _SUBJECT_RECHECKS[subject_type] = recheck
    if lock_key is not None:
        _SUBJECT_LOCK_KEYS[subject_type] = lock_key


def clear_integration_registrations() -> None:
    """Remove every registered adapter and authenticator."""
    _SEND_ADAPTERS.clear()
    _CALLBACK_AUTHENTICATORS.clear()


def _trusted_gucs() -> tuple[UUID, UUID]:
    """Read organization and actor from the transaction-scoped GUCs."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT NULLIF(current_setting('app.current_tenant', true), ''), "
            "NULLIF(current_setting('app.current_user_id', true), '')"
        )
        row = cursor.fetchone()
    if row is None or row[0] is None or row[1] is None:
        raise IntegrationContextError
    try:
        organization_id = UUID(row[0])
        actor_id = UUID(row[1])
    except ValueError as error:
        raise IntegrationContextError from error
    return organization_id, actor_id


@contextmanager
def _stored_scope_context(scope: OperationScope) -> Iterator[None]:
    """Attribute bookkeeping to stored scope in one outermost transaction.

    Used only after an external authorization decision (provider callback
    authentication or a failed membership recheck). It never reads tenant
    claims from external input and never performs domain reads.
    """
    if connection.in_atomic_block:
        raise IntegrationContextError
    try:
        with transaction.atomic(durable=True), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true), "
                "pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(scope.actor_id), str(scope.organization_id)],
            )
            yield
    finally:
        clear_connection_tenant_gucs()


def _resolve_scope(operation_id: UUID) -> OperationScope | None:
    """Resolve stored tenant scope for one operation through the resolver."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT operation_scope.organization_id, "
            "operation_scope.clinic_id, operation_scope.actor_id "
            "FROM clinic_app.comms_operation_scope(%s) AS operation_scope",
            [str(operation_id)],
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return OperationScope(
        operation_id=operation_id,
        organization_id=row[0],
        clinic_id=row[1],
        actor_id=row[2],
    )


def _resolve_callback_scope(
    provider: str,
    provider_reference: str,
) -> OperationScope | None:
    """Resolve stored scope for one authenticated callback reference.

    Correlation is the stored ``(provider, provider_reference)`` pair only;
    a reference that resolves to anything but exactly one operation is
    rejected rather than silently attributed to a tenant.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT callback_scope.operation_id, "
            "callback_scope.organization_id, callback_scope.clinic_id, "
            "callback_scope.actor_id "
            "FROM clinic_app.comms_operation_callback_scope(%s, %s) "
            "AS callback_scope",
            [provider, provider_reference],
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        return None
    row = rows[0]
    return OperationScope(
        operation_id=row[0],
        organization_id=row[1],
        clinic_id=row[2],
        actor_id=row[3],
    )


def _record_integration_event(
    event_type: str,
    scope: OperationScope,
    *,
    reason_code: str | None = None,
) -> int:
    """Append one fixed-vocabulary integration event in the current context."""
    append = build_integration_audit_event(
        event_type,
        clinic_id=scope.clinic_id,
        affected_record_id=scope.operation_id,
        operation_id=scope.operation_id,
        reason_code=reason_code,
    )
    return record_event(append.event, payload=append.payload)


def enqueue_operation(request: OperationRequest) -> UUID:
    """Record one operation in the tenant transaction; dispatch on commit.

    Organization and actor come from the trusted GUCs, never from caller
    input. A repeated idempotency key returns the stored operation without
    dispatching again.
    """
    if (
        request.channel not in IntegrationOperation.Channel.values
        or type(request.provider) is not str
        or not 1 <= len(request.provider) <= MAX_PROVIDER_LENGTH
        or type(request.subject_type) is not str
        or not 1 <= len(request.subject_type) <= MAX_SUBJECT_TYPE_LENGTH
        or type(request.clinic_id) is not UUID
        or type(request.subject_id) is not UUID
        or type(request.idempotency_key) is not UUID
        or type(request.max_attempts) is not int
        or not 1 <= request.max_attempts <= MAX_ATTEMPTS_BOUND
    ):
        raise IntegrationInputError
    organization_id, actor_id = _trusted_gucs()
    operation, created = IntegrationOperation.objects.get_or_create(
        organization_id=organization_id,
        idempotency_key=request.idempotency_key,
        defaults={
            "clinic_id": request.clinic_id,
            "actor_id": actor_id,
            "channel": request.channel,
            "provider": request.provider,
            "subject_type": request.subject_type,
            "subject_id": request.subject_id,
            "max_attempts": request.max_attempts,
        },
    )
    if not created:
        return operation.pk
    _record_integration_event(
        "comms.operation.enqueued",
        OperationScope(
            operation_id=operation.pk,
            organization_id=organization_id,
            clinic_id=request.clinic_id,
            actor_id=actor_id,
        ),
    )
    operation_id = operation.pk
    transaction.on_commit(lambda: _dispatch(operation_id))
    return operation_id


def _dispatch(operation_id: UUID) -> None:
    """Hand one committed operation to the real task transport.

    A halted live activation refuses the dispatch: the committed
    operation row stays pending and recoverable, but no new job is
    handed to the broker after rollback.
    """
    from ops.release.activation import require_live_runtime  # noqa: PLC0415

    from apps.comms.tasks import (  # noqa: PLC0415 - deferred to avoid a
        execute_operation as execute_operation_task,  # core/comms import cycle
    )

    require_live_runtime(os.environ)
    execute_operation_task.apply_async(kwargs={"operation_id": str(operation_id)})


def _actor_has_clinic_authority(scope: OperationScope) -> bool:
    """Recheck the stored actor's membership in the operation's clinic.

    Clinic membership is the baseline authority for the send; an
    organization-level membership in another clinic does not satisfy it.
    """
    return UserClinicRole.objects.filter(
        user_id=scope.actor_id,
        organization_id=scope.organization_id,
        clinic_id=scope.clinic_id,
    ).exists()


@contextmanager
def _operation_lock(operation_id: UUID) -> Iterator[bool]:
    """Hold the operation's session advisory lock on a dedicated connection.

    The lock lives on its own connection so it survives the claim
    transaction's commit and is held for the whole send boundary. A
    duplicate delivery that cannot take the lock knows a live execution
    owns the operation; one that can take it knows the previous claimant
    is gone and the in-progress row is abandoned.
    """
    lock_connection = connections.create_connection("default")
    try:
        with lock_connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.pg_try_advisory_lock("
                "pg_catalog.hashtextextended(%s, 0))",
                [f"{_CLAIM_LOCK_NAMESPACE}{operation_id}"],
            )
            row = cursor.fetchone()
        yield row is not None and bool(row[0])
    finally:
        lock_connection.close()


def _claim_row(scope: OperationScope) -> _ClaimDecision:
    """Claim one pending row or bound one in-progress row under lock.

    Reachable only while the caller holds the operation advisory lock, so
    an in-progress row seen here is abandoned by its previous claimant,
    never a live send.
    """
    operation = (
        IntegrationOperation.objects.select_for_update()
        .filter(pk=scope.operation_id)
        .first()
    )
    if operation is None:
        return _ClaimDecision("skipped", "")
    if operation.status not in _CLAIMABLE_STATUSES or (
        operation.not_before is not None and operation.not_before > timezone.now()
    ):
        return _ClaimDecision("skipped", operation.provider)
    if not _actor_has_clinic_authority(scope):
        return _ClaimDecision("revoked", operation.provider)
    if operation.attempt_count >= operation.max_attempts:
        return _ClaimDecision("exhausted", operation.provider)
    operation.attempt_count += 1
    if operation.status == _PENDING:
        operation.status = _IN_PROGRESS
        operation.save(update_fields=("status", "attempt_count", "updated_at"))
        return _ClaimDecision("send", operation.provider)
    operation.save(update_fields=("attempt_count", "updated_at"))
    return _ClaimDecision("reconcile", operation.provider)


def _persisted_outcome(scope: OperationScope) -> ExecutionResult:
    """Report the stored status after a guarded completion touched no row."""
    with _stored_scope_context(scope):
        status = (
            IntegrationOperation.objects.filter(pk=scope.operation_id)
            .values_list("status", flat=True)
            .first()
        )
    if status == _FAILED:
        return "failed"
    if status == _CANCELLED:
        return "cancelled"
    if status in (_SUCCEEDED, _DELIVERED):
        return "succeeded"
    if status is None:
        return "missing"
    return "reconcile"


def _operation_for_prepare(scope: OperationScope) -> IntegrationOperation:
    """Reload the claimed row inside the tenant transaction for prepare."""
    operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
    if operation is None:
        raise IntegrationContextError
    return operation


def _succeed(scope: OperationScope, provider_reference: str) -> bool:
    """Record provider acceptance once, guarded by the claimed status."""
    with _stored_scope_context(scope):
        updated = IntegrationOperation.objects.filter(
            pk=scope.operation_id,
            status=_IN_PROGRESS,
        ).update(
            status=_SUCCEEDED,
            provider_reference=provider_reference,
            last_error="",
            updated_at=timezone.now(),
        )
        if updated == 1:
            _record_integration_event("comms.operation.succeeded", scope)
            return True
    return False


def _release_for_retry(scope: OperationScope) -> bool:
    """Return one claimed row to pending after a transient send failure."""
    with _stored_scope_context(scope):
        updated = IntegrationOperation.objects.filter(
            pk=scope.operation_id,
            status=_IN_PROGRESS,
        ).update(
            status=_PENDING,
            last_error="transient_send",
            updated_at=timezone.now(),
        )
    return updated == 1


def _fail_operation(scope: OperationScope, *, reason_code: str) -> bool:
    """Fail one claimable operation once and audit the terminal transition."""
    with _stored_scope_context(scope):
        updated = IntegrationOperation.objects.filter(
            pk=scope.operation_id,
            status__in=_CLAIMABLE_STATUSES,
        ).update(
            status=_FAILED,
            last_error=reason_code,
            updated_at=timezone.now(),
        )
        if updated == 1:
            _record_integration_event(
                "comms.operation.failed",
                scope,
                reason_code=reason_code,
            )
            return True
    return False


def _subject_eligible(scope: OperationScope) -> bool:
    """Run the registered send-time recheck for the operation's subject.

    Subject types without a registered recheck are eligible by default;
    a registered recheck that cannot confirm current eligibility fails
    closed.
    """
    operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
    if operation is None:
        return False
    recheck = _SUBJECT_RECHECKS.get(operation.subject_type)
    if recheck is None:
        return True
    return bool(recheck(scope))


def _cancel_ineligible(scope: OperationScope) -> bool:
    """Cancel one claimable operation only while its subject is ineligible."""
    with _stored_scope_context(scope):
        if _subject_eligible(scope):
            return False
        updated = IntegrationOperation.objects.filter(
            pk=scope.operation_id,
            status__in=_CLAIMABLE_STATUSES,
        ).update(
            status=_CANCELLED,
            last_error="subject_ineligible",
            updated_at=timezone.now(),
        )
        if updated == 1:
            _record_integration_event(
                "comms.operation.cancelled",
                scope,
                reason_code="subject_ineligible",
            )
            return True
    return False


def _cancel_revoked(scope: OperationScope) -> bool:
    """Cancel one operation only while the stored actor lacks clinic authority.

    Uses the same clinic-membership predicate as the execution recheck so
    an actor who retains membership in another clinic of the organization
    is still treated as revoked for this operation.
    """
    with _stored_scope_context(scope):
        if _actor_has_clinic_authority(scope):
            return False
        updated = IntegrationOperation.objects.filter(
            pk=scope.operation_id,
            status__in=_CLAIMABLE_STATUSES,
        ).update(
            status=_CANCELLED,
            last_error="authority_revoked",
            updated_at=timezone.now(),
        )
        if updated == 1:
            _record_integration_event(
                "comms.operation.cancelled",
                scope,
                reason_code="authority_revoked",
            )
            return True
    return False


def _claim_subject_lock(scope: OperationScope) -> str | None:
    """Take the session lock on the operation's subject for the send.

    The lock lives on the worker connection outside any transaction so
    it is held from the final eligibility recheck until the provider
    answers; subject mutations hold the matching transaction lock, so
    their commit either precedes the recheck or follows the send.
    """
    with _stored_scope_context(scope):
        operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
        if operation is None:
            return None
        key = (
            f"{_SUBJECT_LOCK_NAMESPACE}{operation.subject_type}:{operation.subject_id}"
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_advisory_lock(pg_catalog.hashtextextended(%s, 0))",
            [key],
        )
    return key


def _release_subject_lock(key: str) -> None:
    """Release the subject session lock after the provider answered.

    A broken connection already dropped the session lock, so a database
    error here must never mask the recorded send outcome.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.pg_advisory_unlock("
                "pg_catalog.hashtextextended(%s, 0))",
                [key],
            )
    except DatabaseError:
        pass


def _presend_decision(scope: OperationScope) -> _PresendDecision:
    """Recheck status, actor authority and subject eligibility under the lock."""
    with _stored_scope_context(scope):
        operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
        if operation is None:
            return "missing"
        if operation.status != _IN_PROGRESS:
            return "ineligible"
        if not _actor_has_clinic_authority(scope):
            return "revoked"
        if not _subject_eligible(scope):
            return "ineligible"
        return "send"


def _decision_outcome(
    scope: OperationScope,
    decision: _PresendDecision,
) -> ExecutionResult:
    """Map one non-send presend decision to its reconciled outcome."""
    if decision == "revoked":
        return "cancelled" if _cancel_revoked(scope) else _persisted_outcome(scope)
    if decision == "ineligible":
        return "cancelled" if _cancel_ineligible(scope) else _persisted_outcome(scope)
    return _persisted_outcome(scope)


def _claim_boundary_lock(scope: OperationScope) -> str | None:
    """Take the session lock on the subject's stable mutation boundary.

    The registered resolver derives the key from the subject row, so a
    subject created while a mutation was in flight still resolves to
    the same lock the mutation holds. The lock lives on the worker
    connection outside any transaction and is held from the second
    recheck until the provider answers.
    """
    with _stored_scope_context(scope):
        operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
        if operation is None:
            return None
        resolver = _SUBJECT_LOCK_KEYS.get(operation.subject_type)
        if resolver is None:
            return None
        lock_key = resolver(scope, operation.subject_id)
        if lock_key is None:
            return None
        key = f"{_SUBJECT_LOCK_NAMESPACE}{lock_key}"
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.pg_advisory_lock(pg_catalog.hashtextextended(%s, 0))",
            [key],
        )
    return key


def _release_boundary_lock(key: str | None) -> None:
    """Release the mutation-boundary session lock after the provider answered."""
    if key is not None:
        _release_subject_lock(key)


def _send_and_finish(
    scope: OperationScope,
    adapter: SendAdapter,
    prepared: object,
) -> ExecutionResult:
    """Recheck under the session locks, send with no transaction, reconcile.

    The subject session lock orders the send against mutations of the
    stored subject row. The mutation-boundary session lock, claimed
    after the recheck, orders the send against mutations of the
    subject's stable parent: the boundary re-decides under it, so a
    mutation that commits while the send waits is always seen before
    the external call, and a mutation still in flight waits for the
    provider answer.
    """
    lock_key = _claim_subject_lock(scope)
    if lock_key is None:
        return _persisted_outcome(scope)
    try:
        decision = _presend_decision(scope)
        if decision != "send":
            return _decision_outcome(scope, decision)
        boundary_key = _claim_boundary_lock(scope)
        try:
            decision = _presend_decision(scope)
            if decision == "send":
                return _deliver(scope, adapter, prepared)
            return _decision_outcome(scope, decision)
        finally:
            _release_boundary_lock(boundary_key)
    finally:
        _release_subject_lock(lock_key)


def _deliver(
    scope: OperationScope,
    adapter: SendAdapter,
    prepared: object,
) -> ExecutionResult:
    """Perform the external call with no open transaction, then reconcile."""
    outcome: ExecutionResult
    try:
        result = adapter.send(prepared, operation_id=scope.operation_id)
    except TransientSendError:
        outcome = "retry" if _release_for_retry(scope) else _persisted_outcome(scope)
    except PermanentSendError:
        outcome = (
            "failed"
            if _fail_operation(scope, reason_code="provider_rejected")
            else _persisted_outcome(scope)
        )
    else:
        provider_reference = result.provider_reference
        if (
            type(provider_reference) is not str
            or not 1 <= len(provider_reference) <= MAX_PROVIDER_REFERENCE_LENGTH
        ):
            outcome = (
                "failed"
                if _fail_operation(scope, reason_code="invalid_provider_reference")
                else _persisted_outcome(scope)
            )
        elif _succeed(scope, provider_reference):
            outcome = "succeeded"
        else:
            outcome = _persisted_outcome(scope)
    return outcome


def _prepare_with_authority(
    scope: OperationScope,
    adapter: SendAdapter,
) -> object:
    """Prepare only while actor authority and subject eligibility hold."""
    if not _actor_has_clinic_authority(scope):
        raise _ClinicAuthorityRevokedError
    if not _subject_eligible(scope):
        raise _SubjectIneligibleError
    return adapter.prepare(_operation_for_prepare(scope))


def _send_claimed(scope: OperationScope, provider: str) -> ExecutionResult:
    """Prepare inside the tenant transaction, then send outside it."""
    adapter = _SEND_ADAPTERS.get(provider)
    if adapter is None:
        return (
            "failed"
            if _fail_operation(scope, reason_code="adapter_unavailable")
            else _persisted_outcome(scope)
        )
    outcome: ExecutionResult
    try:
        with tenant_context(scope.actor_id, scope.organization_id):
            prepared = _prepare_with_authority(scope, adapter)
    except (TenantAccessDeniedError, _ClinicAuthorityRevokedError):
        outcome = "cancelled" if _cancel_revoked(scope) else _persisted_outcome(scope)
    except _SubjectIneligibleError:
        outcome = (
            "cancelled" if _cancel_ineligible(scope) else _persisted_outcome(scope)
        )
    except PermanentSendError:
        outcome = (
            "failed"
            if _fail_operation(scope, reason_code="prepare_failed")
            else _persisted_outcome(scope)
        )
    except TransientSendError:
        outcome = "retry" if _release_for_retry(scope) else _persisted_outcome(scope)
    else:
        outcome = _send_and_finish(scope, adapter, prepared)
    return outcome


def execute_operation(operation_id: UUID) -> ExecutionResult:
    """Execute one stored operation through the trusted worker boundary.

    The operation advisory lock is taken on a dedicated connection before
    the claim and held until completion is recorded, so a duplicate
    delivery can never claim, exhaust or terminally fail an operation
    whose send is still in flight.

    A halted live activation raises ``LiveModeHaltedError`` before any
    stored operation is touched: rollback stops new job execution in
    already-running workers, and the operation row stays recoverable.
    """
    from ops.release.activation import require_live_runtime  # noqa: PLC0415

    require_live_runtime(os.environ)
    scope = _resolve_scope(operation_id)
    if scope is None:
        return "missing"
    with _operation_lock(operation_id) as acquired:
        if not acquired:
            return "reconcile"
        try:
            with tenant_context(scope.actor_id, scope.organization_id):
                decision = _claim_row(scope)
        except TenantAccessDeniedError:
            decision = _ClaimDecision("revoked", "")
        outcome: ExecutionResult
        if decision.outcome == "revoked":
            outcome = (
                "cancelled" if _cancel_revoked(scope) else _persisted_outcome(scope)
            )
        elif decision.outcome == "exhausted":
            outcome = (
                "failed"
                if _fail_operation(scope, reason_code="attempts_exhausted")
                else _persisted_outcome(scope)
            )
        elif decision.outcome == "send":
            outcome = _send_claimed(scope, decision.provider)
        else:
            outcome = decision.outcome
        return outcome


def receive_provider_callback(
    *,
    provider: str,
    headers: Mapping[str, str],
    body: bytes,
) -> CallbackResult:
    """Apply one authenticated provider callback to its stored operation.

    Authentication runs before any stored operation or tenant is resolved;
    only the verified event id, provider reference and status are used.
    """
    authenticator = _CALLBACK_AUTHENTICATORS.get(provider)
    if authenticator is None:
        raise CallbackAuthenticationError
    callback = authenticator.authenticate(headers=headers, body=body)
    if callback.status not in ("delivered", "failed"):
        return "rejected"
    scope = _resolve_callback_scope(provider, callback.provider_reference)
    if scope is None:
        return "rejected"
    with _stored_scope_context(scope):
        operation = (
            IntegrationOperation.objects.select_for_update()
            .filter(pk=scope.operation_id)
            .first()
        )
        if (
            operation is None
            or operation.status in IntegrationOperation.TERMINAL_STATUSES
            or operation.last_callback_event_id == callback.event_id
        ):
            return "duplicate"
        if callback.status == "delivered":
            operation.status = _DELIVERED
            event_type = "comms.operation.delivered"
            reason_code = None
        else:
            operation.status = _FAILED
            event_type = "comms.operation.failed"
            reason_code = "provider_reported"
        operation.last_callback_event_id = callback.event_id
        operation.save(
            update_fields=(
                "status",
                "last_callback_event_id",
                "updated_at",
            )
        )
        _record_integration_event(event_type, scope, reason_code=reason_code)
        return "applied"
