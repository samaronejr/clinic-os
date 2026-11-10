"""Signed partner webhooks delivered through the shared action outbox.

The database captures each event in the source transaction and enqueues one
``action`` operation per subscription, attributed to the client's approving
administrator. At send time the outbox rechecks that administrator, this module
rechecks the subscription, client and scope, reloads the immutable delivery,
verifies its payload digest, signs the exact body and posts it. Every attempt is
logged without URL, body or patient data; a failed attempt returns the operation
to the outbox, whose ``not_before`` backoff the database sets.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import rfc8785
from django.db import connection
from django.utils import timezone

from apps.comms.adapters import (
    OperationScope,
    PermanentSendError,
    SendResult,
    TransientSendError,
)
from apps.comms.models import IntegrationOperation
from apps.core.secrets import secret_store
from apps.interop.external_ids import encoder
from apps.interop.models import EVENT_SCOPES, WebhookDelivery, WebhookSubscription
from apps.interop.webhook_signature import HEADER, signature_header
from apps.interop.webhook_transport import WebhookTransportUnavailableError, transport

if TYPE_CHECKING:
    from uuid import UUID

    from apps.interop.webhook_transport import TransportResult

PROVIDER: Final = "interop-webhook-v1"
SUBJECT_TYPE: Final = "interop.webhook_delivery"
SIGNING_SECRET_NAME: Final = "interop-webhook-signing"  # noqa: S105 - a secret name
SEND_TIMEOUT_SECONDS: Final = 10.0
_SECRET_DOMAIN: Final = b"clinic-webhook-secret-v1\0"
_PUBLIC_STATUS: Final = {"scheduled": "booked"}

type JsonValue = (
    bool | int | str | float | None | Sequence[JsonValue] | Mapping[str, JsonValue]
)


def signing_secret(subscription_id: UUID, version: int) -> str:
    """Derive one subscription signing secret version; nothing is stored."""
    master = bytes.fromhex(secret_store().get_secret(SIGNING_SECRET_NAME))
    material = _SECRET_DOMAIN + subscription_id.bytes + version.to_bytes(4, "big")
    return "whsec_" + hmac.digest(master, material, hashlib.sha256).hex()


def valid_secrets(subscription: WebhookSubscription) -> tuple[str, ...]:
    """Return the current secret and, during the rotation overlap, the previous."""
    current = signing_secret(subscription.pk, subscription.secret_version)
    previous_until = subscription.previous_secret_until
    if (
        subscription.secret_version > 1
        and previous_until is not None
        and previous_until > timezone.now()
    ):
        return (
            current,
            signing_secret(subscription.pk, subscription.secret_version - 1),
        )
    return (current,)


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def public_status(stored: str) -> str:
    """Map stored lifecycle values to the documented external vocabulary."""
    return _PUBLIC_STATUS.get(stored, stored)


def delivery_digest(delivery: WebhookDelivery) -> str:
    """Recompute the digest the database bound into the outbox operation."""
    material = (
        f"clinic-webhook-delivery-v1:{delivery.pk}:{delivery.event_type}:"
        f"{delivery.subject_id}:{delivery.subject_state}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def delivery_payload(delivery: WebhookDelivery) -> dict[str, JsonValue]:
    """Build the event body from immutable delivery facts and opaque ids only."""
    encode = encoder(delivery.organization_id)
    data: dict[str, JsonValue]
    if delivery.event_type.startswith("appointment."):
        data = {
            "appointment": encode("appointment", delivery.subject_id),
            "status": public_status(delivery.subject_state),
            "start_at": _utc(delivery.starts_at),
            "end_at": _utc(delivery.ends_at),
        }
    else:
        data = {
            "invoice": encode("invoice", delivery.subject_id),
            "status": delivery.subject_state,
        }
    return {
        "id": str(delivery.pk),
        "type": delivery.event_type,
        "created": _utc(delivery.occurred_at),
        "data": data,
    }


def delivery_lock_key(scope: OperationScope, subject_id: UUID) -> str | None:
    """Serialize sends with revocation of the delivering client."""
    del scope
    client = (
        WebhookDelivery.objects.filter(pk=subject_id)
        .values_list("subscription__client_id", flat=True)
        .first()
    )
    return None if client is None else f"interop-client:{client}"


def delivery_eligible(scope: OperationScope) -> bool:
    """Recheck subscription, client, scope and the approver's org authority."""
    operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
    if operation is None:
        return False
    delivery = (
        WebhookDelivery.objects.select_related("subscription", "subscription__client")
        .filter(pk=operation.subject_id, operation_id=operation.pk)
        .first()
    )
    if delivery is None:
        return False
    subscription = delivery.subscription
    client = subscription.client
    if (
        subscription.state != "active"
        or client.state != "active"
        or client.approved_by_id != scope.actor_id
        or EVENT_SCOPES.get(delivery.event_type) not in client.scopes
    ):
        return False
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.interop_manager(%s)", [scope.organization_id])
        row = cursor.fetchone()
    return row is not None and row[0] is True


@dataclass(frozen=True, slots=True)
class PreparedWebhook:
    """Everything the send needs, loaded inside the tenant transaction."""

    operation_id: UUID
    delivery_id: UUID
    event_type: str
    url: str
    body: bytes
    secrets: tuple[str, ...]


def _now() -> int:
    """Return the signing clock; tests freeze this seam."""
    return int(time.time())


def _record_attempt(operation_id: UUID, result: TransportResult) -> None:
    """Append the attempt to the delivery log while the operation is in flight."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.interop_webhook_attempt(%s,%s,%s)",
            [operation_id, result.outcome, result.http_status],
        )


class WebhookActionAdapter:
    """Prepare inside the tenant transaction; sign and post outside it."""

    provider = PROVIDER

    def prepare(self, operation: IntegrationOperation) -> PreparedWebhook:
        """Reload the immutable delivery and refuse any digest drift."""
        delivery = (
            WebhookDelivery.objects.select_related("subscription")
            .filter(
                pk=operation.subject_id,
                operation_id=operation.pk,
                organization_id=operation.organization_id,
            )
            .first()
        )
        if delivery is None:
            raise PermanentSendError
        digest = delivery_digest(delivery)
        if digest != delivery.payload_digest or digest != operation.payload_digest:
            raise PermanentSendError
        return PreparedWebhook(
            operation_id=operation.pk,
            delivery_id=delivery.pk,
            event_type=delivery.event_type,
            url=delivery.subscription.url,
            body=rfc8785.dumps(delivery_payload(delivery)),
            secrets=valid_secrets(delivery.subscription),
        )

    def send(self, prepared: object, *, operation_id: UUID) -> SendResult:
        """Sign at send time; a non-2xx answer returns the operation to the outbox."""
        if (
            not isinstance(prepared, PreparedWebhook)
            or prepared.operation_id != operation_id
        ):
            raise PermanentSendError
        try:
            channel = transport()
        except WebhookTransportUnavailableError as error:
            raise PermanentSendError from error
        headers = {
            "Content-Type": "application/json",
            HEADER: signature_header(prepared.secrets, _now(), prepared.body),
            "Clinic-Webhook-Id": str(prepared.delivery_id),
            "Clinic-Webhook-Event": prepared.event_type,
        }
        result = channel.post(
            prepared.url,
            headers=headers,
            body=prepared.body,
            timeout=SEND_TIMEOUT_SECONDS,
        )
        _record_attempt(operation_id, result)
        if result.outcome == "delivered":
            return SendResult(provider_reference=f"webhook:{prepared.delivery_id}")
        raise TransientSendError
