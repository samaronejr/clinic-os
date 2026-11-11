"""Organization-administrator management of partner clients, webhooks and exports.

Every service is keyword-only and clinic-scoped. Authority is
``configuration.organization`` on every clinic of the organization, checked in
Python and again by the ``interop_manager`` resolver; unknown and foreign
selectors share one denial. Secrets are generated here, shown once and only
their SHA-256 digests reach the database.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

import rfc8785
from django.db import DatabaseError, connection, transaction
from django.db.models.functions import Now

from apps.audit.services import record_phase1_event
from apps.comms.models import IntegrationOperation
from apps.identity.current_context import CurrentActorError, require_permission
from apps.interop.credentials import client_secret_digest, new_client_secret
from apps.interop.errors import (
    InteropAccessDeniedError,
    InteropConflictError,
    InteropInputError,
)
from apps.interop.models import (
    SCOPES,
    ApiClient,
    OrganizationExport,
    OrganizationExportPart,
    WebhookAttempt,
    WebhookDelivery,
    WebhookSubscription,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from apps.interop.webhooks import JsonValue

__all__ = (
    "ClientView",
    "CommandResult",
    "DeliveryView",
    "ExportPartDownload",
    "ExportView",
    "IssuedCredential",
    "WebhookView",
    "create_client",
    "download_export_part",
    "list_clients",
    "list_deliveries",
    "list_exports",
    "list_webhooks",
    "request_export",
    "revoke_client",
    "revoke_webhook",
    "rotate_client_secret",
)

MAX_NAME_LENGTH: Final = 80
MAX_RATE_LIMIT: Final = 6000
MAX_LISTED: Final = 200
MAX_REVISION: Final = 2**31 - 1
_DELIVERY_STATES: Final = {
    "pending": "pending",
    "in_progress": "pending",
    "succeeded": "delivered",
    "delivered": "delivered",
    "failed": "failed",
    "cancelled": "cancelled",
}
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")
# Constraint, cast and uniqueness refusals of bounded caller terms.
_INPUT_SQLSTATES: Final = frozenset({"22001", "22003", "22P02", "23505", "23514"})


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """A client id with its secret; the secret is never retrievable again."""

    client_id: UUID
    client_secret: str
    revision: int


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The stored revision and state a management command produced."""

    record_id: UUID
    revision: int
    state: str


@dataclass(frozen=True, slots=True)
class ClientView:
    """Partner client metadata; credentials are never listed."""

    client_id: UUID
    clinic_id: UUID
    name: str
    scopes: tuple[str, ...]
    rate_limit_per_minute: int
    state: str
    revision: int
    created_at: datetime
    revoked_at: datetime | None
    revocation_reason: str


@dataclass(frozen=True, slots=True)
class WebhookView:
    """One partner endpoint registration."""

    subscription_id: UUID
    client_id: UUID
    clinic_id: UUID
    url: str
    events: tuple[str, ...]
    state: str
    revision: int
    secret_version: int
    created_at: datetime
    revoked_at: datetime | None
    revocation_reason: str


@dataclass(frozen=True, slots=True)
class AttemptView:
    """One delivery-log line: outcome and status code only."""

    attempt: int
    outcome: str
    http_status: int | None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class DeliveryView:
    """One event delivery with its attempts; no URL, body or patient data."""

    delivery_id: UUID
    event_type: str
    occurred_at: datetime
    state: str
    attempts: tuple[AttemptView, ...]


@dataclass(frozen=True, slots=True)
class ExportView:
    """One organization export receipt."""

    export_id: UUID
    state: str
    revision: int
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime | None
    part_count: int
    archive_bytes: int
    archive_sha256: str
    manifest_sha256: str
    dataset_count: int
    record_count: int
    attachment_count: int
    failure_reason: str


@dataclass(frozen=True, slots=True)
class ExportPartDownload:
    """One decrypted archive byte range and the digests that verify it."""

    export_id: UUID
    part: int
    part_count: int
    data: bytes
    sha256: str
    archive_sha256: str


@dataclass(frozen=True, slots=True)
class _Outcome:
    record_id: UUID
    clinic_id: UUID
    revision: int
    state: str
    changed: bool


def _scope(clinic_id: object) -> UUID:
    """Return the organization when the actor manages every one of its clinics."""
    if type(clinic_id) is not UUID:
        raise InteropAccessDeniedError
    try:
        require_permission("configuration.organization", clinic_id=clinic_id)
    except CurrentActorError:
        raise InteropAccessDeniedError from None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.organization_id FROM clinic_app.identity_clinic c "
            "WHERE c.id=%s AND clinic_app.interop_manager(c.organization_id)",
            [clinic_id],
        )
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], UUID):
        raise InteropAccessDeniedError
    return row[0]


def _command(  # noqa: PLR0913 - the resolver's exact command terms
    *,
    clinic_id: UUID,
    action: str,
    subject: UUID,
    expected_revision: int,
    presented_secret: str = "",
    parameters: str = "{}",
) -> _Outcome:
    """Run one management command in a savepoint; refusals keep the request usable."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.interop_manage(%s,%s,%s,%s,%s,%s::jsonb)",
                [
                    clinic_id,
                    action,
                    subject,
                    expected_revision,
                    presented_secret,
                    parameters,
                ],
            )
            row = cursor.fetchone()
    except DatabaseError as error:
        sqlstate = getattr(error.__cause__, "sqlstate", None)
        if sqlstate == "42501":
            raise InteropAccessDeniedError from None
        if sqlstate in _INPUT_SQLSTATES:
            raise InteropInputError from None
        raise
    if row is None:
        raise InteropConflictError
    record_id, record_clinic, revision, state, changed = row
    return _Outcome(record_id, record_clinic, int(revision), str(state), bool(changed))


def _revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise InteropInputError
    return value


def _uuid(value: object) -> UUID:
    if type(value) is not UUID:
        raise InteropInputError
    return value


def _client_terms(
    name: object, scopes: object, rate_limit_per_minute: object
) -> tuple[str, list[str], int]:
    if (
        not isinstance(name, str)
        or _CONTROL.search(name) is not None
        or unicodedata.normalize("NFC", name.strip()) != name
        or not 1 <= len(name) <= MAX_NAME_LENGTH
    ):
        raise InteropInputError
    if (
        not isinstance(scopes, (list, tuple))
        or not scopes
        or not all(isinstance(scope, str) and scope in SCOPES for scope in scopes)
        or len(set(scopes)) != len(scopes)
    ):
        raise InteropInputError
    if (
        type(rate_limit_per_minute) is not int
        or not 1 <= rate_limit_per_minute <= MAX_RATE_LIMIT
    ):
        raise InteropInputError
    return name, sorted(cast("Sequence[str]", scopes)), rate_limit_per_minute


def _json(value: dict[str, JsonValue]) -> str:
    return rfc8785.dumps(value).decode("utf-8")


def create_client(
    *,
    clinic_id: UUID,
    name: str,
    scopes: Sequence[str],
    rate_limit_per_minute: int,
    command_id: UUID,
) -> IssuedCredential:
    """Register a partner client for one clinic and show its secret once."""
    label, wanted, rate = _client_terms(name, scopes, rate_limit_per_minute)
    command = _uuid(command_id)
    _scope(clinic_id)
    secret = new_client_secret()
    digest = client_secret_digest(secret)
    if digest is None:  # pragma: no cover - a fresh secret is always well formed
        raise InteropInputError
    outcome = _command(
        clinic_id=clinic_id,
        action="client_create",
        subject=command,
        expected_revision=0,
        presented_secret=digest,
        parameters=_json({"name": label, "scopes": wanted, "rate_limit": rate}),
    )
    if not outcome.changed:
        # A replayed create cannot show the original secret again.
        raise InteropConflictError
    record_phase1_event(
        "interop.api_client.created",
        clinic_id=outcome.clinic_id,
        affected_record_id=outcome.record_id,
    )
    return IssuedCredential(outcome.record_id, secret, outcome.revision)


def rotate_client_secret(
    *, clinic_id: UUID, client_id: UUID, expected_revision: int
) -> IssuedCredential:
    """Issue a new secret; the previous one stays valid for 24 hours.

    The rotating administrator becomes the client's approving authority.
    """
    subject = _uuid(client_id)
    revision = _revision(expected_revision)
    _scope(clinic_id)
    secret = new_client_secret()
    digest = client_secret_digest(secret)
    if digest is None:  # pragma: no cover - a fresh secret is always well formed
        raise InteropInputError
    outcome = _command(
        clinic_id=clinic_id,
        action="client_rotate",
        subject=subject,
        expected_revision=revision,
        presented_secret=digest,
    )
    if not outcome.changed:
        raise InteropConflictError
    record_phase1_event(
        "interop.api_client.rotated",
        clinic_id=outcome.clinic_id,
        affected_record_id=outcome.record_id,
    )
    return IssuedCredential(outcome.record_id, secret, outcome.revision)


def revoke_client(
    *, clinic_id: UUID, client_id: UUID, expected_revision: int
) -> CommandResult:
    """Revoke a client, all its tokens and webhook subscriptions, immediately."""
    subject = _uuid(client_id)
    revision = _revision(expected_revision)
    _scope(clinic_id)
    outcome = _command(
        clinic_id=clinic_id,
        action="client_revoke",
        subject=subject,
        expected_revision=revision,
    )
    if outcome.changed:
        record_phase1_event(
            "interop.api_client.revoked",
            clinic_id=outcome.clinic_id,
            affected_record_id=outcome.record_id,
        )
    return CommandResult(outcome.record_id, outcome.revision, outcome.state)


def revoke_webhook(
    *, clinic_id: UUID, subscription_id: UUID, expected_revision: int
) -> CommandResult:
    """Revoke one partner endpoint; queued deliveries cancel at send time."""
    subject = _uuid(subscription_id)
    revision = _revision(expected_revision)
    _scope(clinic_id)
    outcome = _command(
        clinic_id=clinic_id,
        action="webhook_revoke",
        subject=subject,
        expected_revision=revision,
    )
    if outcome.changed:
        record_phase1_event(
            "interop.webhook_subscription.revoked",
            clinic_id=outcome.clinic_id,
            affected_record_id=outcome.record_id,
        )
    return CommandResult(outcome.record_id, outcome.revision, outcome.state)


def request_export(*, clinic_id: UUID, command_id: UUID) -> ExportView:
    """Queue one organization-wide export; any subscription state may export."""
    subject = _uuid(command_id)
    organization_id = _scope(clinic_id)
    outcome = _command(
        clinic_id=clinic_id,
        action="export_request",
        subject=subject,
        expected_revision=0,
    )
    if outcome.changed:
        record_phase1_event(
            "interop.organization_export.requested",
            clinic_id=outcome.clinic_id,
            affected_record_id=outcome.record_id,
        )
        from apps.interop.tasks import dispatch_export  # noqa: PLC0415

        requested_by = (
            OrganizationExport.objects.filter(pk=outcome.record_id)
            .values_list("requested_by_id", flat=True)
            .get()
        )
        transaction.on_commit(
            lambda: dispatch_export(
                export_id=outcome.record_id,
                organization_id=organization_id,
                requested_by_id=requested_by,
            )
        )
    return _export_view(OrganizationExport.objects.get(pk=outcome.record_id))


def list_clients(*, clinic_id: UUID) -> tuple[ClientView, ...]:
    """List the organization's partner clients without any credential."""
    organization_id = _scope(clinic_id)
    return tuple(
        ClientView(
            client.pk,
            client.clinic_id,
            client.name,
            tuple(client.scopes),
            client.rate_limit_per_minute,
            client.state,
            client.revision,
            client.created_at,
            client.revoked_at,
            client.revocation_reason,
        )
        for client in ApiClient.objects.filter(
            organization_id=organization_id
        ).order_by("created_at", "id")[:MAX_LISTED]
    )


def list_webhooks(
    *, clinic_id: UUID, client_id: UUID | None = None
) -> tuple[WebhookView, ...]:
    """List endpoint registrations, optionally for one client."""
    organization_id = _scope(clinic_id)
    rows = WebhookSubscription.objects.filter(organization_id=organization_id)
    if client_id is not None:
        rows = rows.filter(client_id=_uuid(client_id))
    return tuple(
        WebhookView(
            row.pk,
            row.client_id,
            row.clinic_id,
            row.url,
            tuple(row.events),
            row.state,
            row.revision,
            row.secret_version,
            row.created_at,
            row.revoked_at,
            row.revocation_reason,
        )
        for row in rows.order_by("created_at", "id")[:MAX_LISTED]
    )


def list_deliveries(
    *, clinic_id: UUID, subscription_id: UUID
) -> tuple[DeliveryView, ...]:
    """Return the delivery log of one subscription, newest first."""
    organization_id = _scope(clinic_id)
    selected = _uuid(subscription_id)
    deliveries = list(
        WebhookDelivery.objects.filter(
            organization_id=organization_id, subscription_id=selected
        ).order_by("-occurred_at", "-id")[:MAX_LISTED]
    )
    statuses = dict(
        IntegrationOperation.objects.filter(
            organization_id=organization_id,
            pk__in=[delivery.operation_id for delivery in deliveries],
        ).values_list("pk", "status")
    )
    attempts: dict[UUID, list[AttemptView]] = {}
    for attempt in WebhookAttempt.objects.filter(
        organization_id=organization_id,
        delivery_id__in=[delivery.pk for delivery in deliveries],
    ).order_by("attempt"):
        attempts.setdefault(attempt.delivery_id, []).append(
            AttemptView(
                attempt.attempt,
                attempt.outcome,
                attempt.http_status,
                attempt.recorded_at,
            )
        )
    return tuple(
        DeliveryView(
            delivery.pk,
            delivery.event_type,
            delivery.occurred_at,
            _DELIVERY_STATES.get(str(statuses.get(delivery.operation_id)), "pending"),
            tuple(attempts.get(delivery.pk, ())),
        )
        for delivery in deliveries
    )


def _export_view(job: OrganizationExport) -> ExportView:
    return ExportView(
        job.pk,
        job.state,
        job.revision,
        job.created_at,
        job.finished_at,
        job.expires_at,
        job.part_count,
        job.archive_bytes,
        job.archive_sha256,
        job.manifest_sha256,
        job.dataset_count,
        job.record_count,
        job.attachment_count,
        job.failure_reason,
    )


def list_exports(*, clinic_id: UUID) -> tuple[ExportView, ...]:
    """List the organization's export receipts, newest first."""
    organization_id = _scope(clinic_id)
    return tuple(
        _export_view(job)
        for job in OrganizationExport.objects.filter(
            organization_id=organization_id
        ).order_by("-created_at", "-id")[:MAX_LISTED]
    )


def download_export_part(
    *, clinic_id: UUID, export_id: UUID, part: int
) -> ExportPartDownload:
    """Return one decrypted part of a ready, unexpired archive."""
    organization_id = _scope(clinic_id)
    selected = _uuid(export_id)
    if type(part) is not int or not 1 <= part <= MAX_REVISION:
        raise InteropInputError
    job = OrganizationExport.objects.filter(
        pk=selected,
        organization_id=organization_id,
        state="ready",
        expires_at__gt=Now(),
    ).first()
    if job is None or part > job.part_count:
        raise InteropAccessDeniedError
    row = OrganizationExportPart.objects.filter(
        organization_id=organization_id, export_id=job.pk, part=part
    ).first()
    if row is None or row.data is None:
        raise InteropAccessDeniedError
    record_phase1_event(
        "interop.organization_export.downloaded",
        clinic_id=job.clinic_id,
        affected_record_id=job.pk,
    )
    return ExportPartDownload(
        job.pk, part, job.part_count, bytes(row.data), row.sha256, job.archive_sha256
    )
