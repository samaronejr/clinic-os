"""Authenticated, explicitly enabled synthetic delivery receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import TYPE_CHECKING

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.comms.adapters import AuthenticatedCallback, CallbackAuthenticationError
from apps.core.integration import receive_provider_callback
from apps.core.secrets import SecretUnavailableError, secret_store

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.http import HttpRequest

SALT = b"clinic-workflow-synthetic-callback-v1"
MAX_CALLBACK_BYTES = 512


def signing_key() -> bytes:
    """Only the explicitly provisioned managed secret can authenticate receipts."""
    material = secret_store().get_secret("workflow-synthetic-callback")
    return hmac.digest(bytes.fromhex(material), SALT, hashlib.sha256)


def signature(body: bytes) -> str:
    """Authenticate receipt bytes without disclosing key material or contents."""
    return hmac.digest(signing_key(), body, hashlib.sha256).hex()


class SyntheticWorkflowAuthenticator:
    """Accept only authenticated delivery metadata, never payload tenant claims."""

    provider = "workflow-synthetic-v1"

    def authenticate(
        self, *, headers: Mapping[str, str], body: bytes
    ) -> AuthenticatedCallback:
        """Authenticate before parsing or resolving any operation or tenant."""
        if (
            settings.CLINIC_DATA_MODE != "synthetic"
            or not settings.WORKFLOWS_SYNTHETIC_ENABLED
            or len(body) > MAX_CALLBACK_BYTES
        ):
            raise CallbackAuthenticationError
        try:
            expected_signature = signature(body)
        except SecretUnavailableError as error:
            raise CallbackAuthenticationError from error
        if not hmac.compare_digest(
            headers.get("X-Workflow-Signature", ""), expected_signature
        ):
            raise CallbackAuthenticationError
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError) as error:
            raise CallbackAuthenticationError from error
        if (
            not isinstance(value, dict)
            or set(value) != {"event_id", "provider_reference", "status"}
            or not isinstance(value["event_id"], str)
            or re.fullmatch(r"[0-9a-f]{32}", value["event_id"]) is None
            or not isinstance(value["provider_reference"], str)
            or re.fullmatch(
                r"synthetic:workflow:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                value["provider_reference"],
            )
            is None
        ):
            raise CallbackAuthenticationError
        match value["status"]:
            case "delivered":
                return AuthenticatedCallback(
                    value["event_id"], value["provider_reference"], "delivered"
                )
            case "failed":
                return AuthenticatedCallback(
                    value["event_id"], value["provider_reference"], "failed"
                )
            case _:
                # This is the untrusted parse boundary, not a typed variant.
                raise CallbackAuthenticationError


@csrf_exempt
def callback(request: HttpRequest) -> HttpResponse:
    """Authenticate the sessionless receipt before resolving stored scope."""
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        outcome = receive_provider_callback(
            provider="workflow-synthetic-v1",
            headers=request.headers,
            body=request.body,
        )
    except CallbackAuthenticationError:
        return HttpResponse(status=403)
    return HttpResponse(
        json.dumps({"result": outcome}),
        content_type="application/json",
        status=200 if outcome in {"applied", "duplicate"} else 400,
    )
