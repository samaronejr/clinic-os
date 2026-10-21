"""Expose the separately authenticated synthetic laboratory callback."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters

from apps.core.secrets import SecretUnavailableError
from apps.orders.lab import LabCallbackRejectedError, receive_lab_result
from apps.orders.values import OrdersAccessDeniedError, OrdersConflictError

if TYPE_CHECKING:
    from django.http import HttpRequest


@csrf_exempt
@sensitive_post_parameters()
def lab_callback(request: HttpRequest) -> HttpResponse:
    """Authenticate the lab before scope lookup; ignore staff session authority."""
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        receipt = receive_lab_result(headers=request.headers, body=request.body)
    except (LabCallbackRejectedError, OrdersAccessDeniedError, SecretUnavailableError):
        return HttpResponse(status=403)
    except (ValidationError, OrdersConflictError):
        return HttpResponse(status=409)
    except DatabaseError as error:
        state = getattr(error.__cause__, "sqlstate", None)
        if state == "42501":
            return HttpResponse(status=403)
        if state in {"40001", "23514"}:
            return HttpResponse(status=409)
        raise
    return JsonResponse({"version_id": str(receipt.version_id), "state": receipt.state})
