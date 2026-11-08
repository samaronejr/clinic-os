"""Sessionless provider webhook endpoint; authentication is the raw-byte HMAC."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import DatabaseError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters

from apps.comms.inbound_providers import WebhookRejectedError
from apps.comms.inbox_inbound import (
    InboundReceipt,
    InboundRejectedError,
    receive_inbound,
    verify_token_name,
)
from apps.comms.whatsapp_payloads import verification_response
from apps.core.secrets import SecretUnavailableError, secret_store

if TYPE_CHECKING:
    from uuid import UUID

    from django.http import HttpRequest


def _database_status(error: DatabaseError) -> int:
    """Map a guard refusal to a status; any other database failure propagates."""
    state = getattr(error.__cause__, "sqlstate", None)
    if state == "42501":
        return 403
    if state in {"40001", "23514"}:
        return 409
    raise error


def _receipt_response(receipt: InboundReceipt) -> HttpResponse:
    """Return the counts only; a status that raced its send asks for redelivery."""
    if receipt.retry:
        # Nothing was consumed for that event, and applied events in the same
        # body dedupe by their event ids on redelivery.
        response = HttpResponse(status=503)
        response["Retry-After"] = "5"
        return response
    return JsonResponse(
        {
            "accepted": receipt.accepted,
            "duplicates": receipt.duplicates,
            "ignored": receipt.ignored,
        }
    )


def _handshake(request: HttpRequest, account_id: UUID) -> HttpResponse:
    """Answer Meta's subscription challenge from the account's own verify token."""
    try:
        expected = secret_store().get_secret(verify_token_name(account_id))
    except SecretUnavailableError:
        return HttpResponse(status=403)
    answer = verification_response(
        mode=request.GET.get("hub.mode", ""),
        token=request.GET.get("hub.verify_token", ""),
        challenge=request.GET.get("hub.challenge", ""),
        expected_token=expected,
    )
    if answer is None:
        return HttpResponse(status=403)
    return HttpResponse(answer, content_type="text/plain")


@csrf_exempt
@sensitive_post_parameters()
def inbound_webhook(
    request: HttpRequest, provider: str, account_id: UUID
) -> HttpResponse:
    """Authenticate before scope; ignore every staff cookie on this path."""
    if request.method == "GET" and provider == "whatsapp":
        return _handshake(request, account_id)
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        receipt = receive_inbound(
            provider=provider,
            account_id=account_id,
            headers=request.headers,
            body=request.body,
        )
    except (InboundRejectedError, SecretUnavailableError):
        return HttpResponse(status=403)
    except WebhookRejectedError:
        return HttpResponse(status=400)
    except DatabaseError as error:
        return HttpResponse(status=_database_status(error))
    return _receipt_response(receipt)
