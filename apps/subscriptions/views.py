"""Private POST-only commercial API; selectors and commands stay in bodies."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.identity.otp import privileged_totp_required
from apps.subscriptions.models import METERS
from apps.subscriptions.services import (
    SubscriptionConflictError,
    SubscriptionDeniedError,
    SubscriptionInputError,
    cancel_subscription,
    change_plan,
    entitlement_status,
    subscription_snapshot,
    usage_report,
)

if TYPE_CHECKING:
    from django.http import HttpRequest


MAX_BODY_BYTES: Final = 4096


def _url() -> str:
    return reverse("subscriptions:commercial")


def _denied() -> JsonResponse:
    return JsonResponse({"code": "not_available"}, status=403)


def _body(request: HttpRequest) -> dict[str, object]:
    # Commands are JSON only; a multipart body is already consumed as POST data.
    if request.content_type != "application/json" or len(request.body) > MAX_BODY_BYTES:
        raise SubscriptionInputError
    body = json.loads(request.body)
    if not isinstance(body, dict):
        raise SubscriptionInputError
    return cast("dict[str, object]", body)


def _mutate(body: dict[str, object], clinic_id: UUID) -> JsonResponse:
    action = body.get("action")
    names = {"clinic_id", "action", "command_id", "expected_revision"}
    if action == "change_plan":
        names.add("plan_code")
    if set(body) != names or action not in {"change_plan", "cancel"}:
        return _denied()
    revision, command_key = body["expected_revision"], body["command_id"]
    if type(revision) is not int or not isinstance(command_key, str):
        raise SubscriptionInputError
    command_id = UUID(command_key)
    if action == "change_plan":
        plan_code = body["plan_code"]
        if not isinstance(plan_code, str):
            raise SubscriptionInputError
        result = change_plan(
            clinic_id=clinic_id,
            plan_code=plan_code,
            expected_revision=revision,
            command_id=command_id,
        )
    else:
        result = cancel_subscription(
            clinic_id=clinic_id,
            expected_revision=revision,
            command_id=command_id,
        )
    return JsonResponse({"result": asdict(result)})


@privileged_totp_required(_url)
@sensitive_post_parameters()
@require_POST
def commercial(request: HttpRequest) -> HttpResponse:
    """Permit package selection, never client-supplied commercial authority."""
    try:
        body = _body(request)
        selected_clinic = body.get("clinic_id")
        if not isinstance(selected_clinic, str):
            return _denied()
        clinic_id = UUID(selected_clinic)
        # Decide the read/response authority before any command can write.
        report = usage_report(clinic_id=clinic_id)
        action = body.get("action")
        if action == "read" and set(body) == {"clinic_id", "action"}:
            return JsonResponse(
                {
                    "subscription": asdict(subscription_snapshot(clinic_id=clinic_id)),
                    "usage": report,
                    "allowances": {
                        meter: asdict(
                            entitlement_status(
                                clinic_id=clinic_id, meter=meter, quantity=1
                            )
                        )
                        for meter in METERS
                    },
                }
            )
        return _mutate(body, clinic_id)
    except SubscriptionDeniedError:
        return _denied()
    except SubscriptionConflictError:
        return JsonResponse({"code": "conflict"}, status=409)
    except (SubscriptionInputError, ValueError, TypeError, KeyError):
        return JsonResponse({"code": "invalid_input"}, status=400)
