"""Private POST-only management API for organization administrators.

``POST /api/interop/v1/`` takes JSON with ``clinic_id`` and one ``action``.
Selectors travel in the body; refusals never write the session or cookies and
unknown, foreign and unauthorized selectors share one denial body.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID

from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from apps.identity.otp import privileged_totp_required
from apps.interop import management
from apps.interop.errors import (
    InteropAccessDeniedError,
    InteropConflictError,
    InteropInputError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest

MAX_BODY_BYTES: Final = 8192


def _url() -> str:
    return reverse("interop:management")


def _denied() -> JsonResponse:
    return JsonResponse({"code": "not_available"}, status=403)


def _body(request: HttpRequest) -> dict[str, object]:
    if request.content_type != "application/json" or len(request.body) > MAX_BODY_BYTES:
        raise InteropInputError
    body = json.loads(request.body)
    if not isinstance(body, dict):
        raise InteropInputError
    return cast("dict[str, object]", body)


def _uuid(body: dict[str, object], key: str) -> UUID:
    value = body.get(key)
    if not isinstance(value, str):
        raise InteropInputError
    return UUID(value)


def _int(body: dict[str, object], key: str) -> int:
    value = body.get(key)
    if type(value) is not int:
        raise InteropInputError
    return value


def _fields(body: dict[str, object], *names: str) -> None:
    if set(body) != {"clinic_id", "action", *names}:
        raise InteropInputError


def _json(value: object) -> JsonResponse:
    return JsonResponse(value, safe=False, json_dumps_params=None)


def _clients(body: dict[str, object], clinic_id: UUID) -> HttpResponse:
    _fields(body)
    return _json(
        {
            "clients": [
                asdict(row) for row in management.list_clients(clinic_id=clinic_id)
            ]
        }
    )


def _create(body: dict[str, object], clinic_id: UUID) -> HttpResponse:
    _fields(body, "name", "scopes", "rate_limit_per_minute", "command_id")
    issued = management.create_client(
        clinic_id=clinic_id,
        name=cast("str", body["name"]),
        scopes=cast("list[str]", body["scopes"]),
        rate_limit_per_minute=_int(body, "rate_limit_per_minute"),
        command_id=_uuid(body, "command_id"),
    )
    return JsonResponse(asdict(issued), status=201)


def _rotate(body: dict[str, object], clinic_id: UUID) -> HttpResponse:
    _fields(body, "client_id", "expected_revision")
    issued = management.rotate_client_secret(
        clinic_id=clinic_id,
        client_id=_uuid(body, "client_id"),
        expected_revision=_int(body, "expected_revision"),
    )
    return _json(asdict(issued))


def _revoke(body: dict[str, object], clinic_id: UUID) -> HttpResponse:
    _fields(body, "client_id", "expected_revision")
    result = management.revoke_client(
        clinic_id=clinic_id,
        client_id=_uuid(body, "client_id"),
        expected_revision=_int(body, "expected_revision"),
    )
    return _json(asdict(result))


def _webhooks(body: dict[str, object], clinic_id: UUID) -> HttpResponse:
    if "client_id" in body:
        _fields(body, "client_id")
        rows = management.list_webhooks(
            clinic_id=clinic_id, client_id=_uuid(body, "client_id")
        )
    else:
        _fields(body)
        rows = management.list_webhooks(clinic_id=clinic_id)
    return _json({"subscriptions": [asdict(row) for row in rows]})


def _webhook_revoke(body: dict[str, object], clinic_id: UUID) -> HttpResponse:
    _fields(body, "subscription_id", "expected_revision")
    result = management.revoke_webhook(
        clinic_id=clinic_id,
        subscription_id=_uuid(body, "subscription_id"),
        expected_revision=_int(body, "expected_revision"),
    )
    return _json(asdict(result))


def _deliveries(body: dict[str, object], clinic_id: UUID) -> HttpResponse:
    _fields(body, "subscription_id")
    rows = management.list_deliveries(
        clinic_id=clinic_id, subscription_id=_uuid(body, "subscription_id")
    )
    return _json({"deliveries": [asdict(row) for row in rows]})


def _export_request(body: dict[str, object], clinic_id: UUID) -> HttpResponse:
    _fields(body, "command_id")
    job = management.request_export(
        clinic_id=clinic_id, command_id=_uuid(body, "command_id")
    )
    return JsonResponse(asdict(job), status=202)


def _exports(body: dict[str, object], clinic_id: UUID) -> HttpResponse:
    _fields(body)
    return _json(
        {
            "exports": [
                asdict(row) for row in management.list_exports(clinic_id=clinic_id)
            ]
        }
    )


def _download(body: dict[str, object], clinic_id: UUID) -> HttpResponse:
    _fields(body, "export_id", "part")
    part = management.download_export_part(
        clinic_id=clinic_id,
        export_id=_uuid(body, "export_id"),
        part=_int(body, "part"),
    )
    response = HttpResponse(
        part.data,
        content_type="application/zip"
        if part.part_count == 1
        else "application/octet-stream",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="organizacao-{part.export_id}-parte-{part.part}.bin"'
        if part.part_count > 1
        else f'attachment; filename="organizacao-{part.export_id}.zip"'
    )
    response["X-Export-Part"] = f"{part.part}/{part.part_count}"
    response["X-Export-Part-Sha256"] = part.sha256
    response["X-Export-Archive-Sha256"] = part.archive_sha256
    return response


ACTIONS: Final[dict[str, Callable[[dict[str, object], UUID], HttpResponse]]] = {
    "clients.list": _clients,
    "clients.create": _create,
    "clients.rotate": _rotate,
    "clients.revoke": _revoke,
    "webhooks.list": _webhooks,
    "webhooks.revoke": _webhook_revoke,
    "deliveries.list": _deliveries,
    "exports.request": _export_request,
    "exports.list": _exports,
    "exports.download": _download,
}


@never_cache
@privileged_totp_required(_url)
@sensitive_post_parameters()
@require_POST
def manage(request: HttpRequest) -> HttpResponse:
    """Dispatch one closed management action under current org authority."""
    try:
        body = _body(request)
        action = ACTIONS.get(str(body.get("action")))
        if action is None:
            return _denied()
        return action(body, _uuid(body, "clinic_id"))
    except InteropAccessDeniedError:
        return _denied()
    except InteropConflictError:
        return JsonResponse({"code": "conflict"}, status=409)
    except (InteropInputError, ValueError, TypeError, KeyError):
        return JsonResponse({"code": "invalid_input"}, status=400)
