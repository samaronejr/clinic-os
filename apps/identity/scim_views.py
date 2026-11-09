"""Sessionless SCIM core subset, authenticated solely by a scoped bearer."""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from typing import TYPE_CHECKING, cast
from uuid import UUID

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.identity.enterprise_authority import GovernanceDeniedError
from apps.identity.otp import auth_response
from apps.identity.scim import (
    create_resource,
    resource_document,
    search_resources,
    update_resource,
)
from apps.identity.scim_context import ScimRateLimitError, scim_context
from apps.identity.scim_schema import (
    MAX_RESOURCE_BYTES,
    ScimConflictError,
    ScimInputError,
    patched,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.http.response import HttpResponseBase

ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


def _error(status: int, scim_type: str = "") -> HttpResponseBase:
    payload = {"schemas": [ERROR_SCHEMA], "status": str(status)}
    if scim_type:
        payload["scimType"] = scim_type
    response = JsonResponse(
        payload, status=status, content_type="application/scim+json"
    )
    if status == HTTPStatus.UNAUTHORIZED:
        response["WWW-Authenticate"] = "Bearer"
    return auth_response(response)


def _body(request: HttpRequest) -> dict[str, object]:
    if len(request.body) > MAX_RESOURCE_BYTES or request.content_type not in {
        "application/scim+json",
        "application/json",
    }:
        raise ScimInputError
    try:
        result = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ScimInputError from error
    if not isinstance(result, dict):
        raise ScimInputError
    return result


def _expected_revision(request: HttpRequest) -> int | None:
    value = request.headers.get("If-Match")
    if value is None:
        return None
    matched = re.fullmatch(r'W/"([1-9][0-9]{0,9})"', value)
    if matched is None:
        raise ScimInputError
    return int(matched.group(1))


def _search(request: HttpRequest, *, clinic_id: UUID, kind: str) -> HttpResponseBase:
    if request.method != "POST":
        return _error(405)
    body = _body(request)
    if set(body) - {"schemas", "filter", "startIndex", "count"}:
        raise ScimInputError
    filter_text = body.get("filter", "")
    start_index, count = body.get("startIndex", 1), body.get("count", 100)
    if (
        not isinstance(filter_text, str)
        or type(start_index) is not int
        or type(count) is not int
    ):
        raise ScimInputError
    return auth_response(
        JsonResponse(
            search_resources(
                clinic_id=clinic_id,
                kind=kind,
                filter_text=filter_text,
                start_index=start_index,
                count=count,
            ),
            content_type="application/scim+json",
        )
    )


def _document_response(
    *, clinic_id: UUID, kind: str, resource_id: UUID, status: int = 200
) -> HttpResponseBase:
    payload = resource_document(clinic_id=clinic_id, resource_id=resource_id, kind=kind)
    metadata = cast("Mapping[str, str]", payload["meta"])
    response = JsonResponse(
        payload, status=status, content_type="application/scim+json"
    )
    response["Location"] = metadata["location"]
    response["ETag"] = metadata["version"]
    return auth_response(response)


def _collection(
    request: HttpRequest, *, clinic_id: UUID, kind: str
) -> HttpResponseBase:
    if request.method == "POST":
        resource = create_resource(
            clinic_id=clinic_id, kind=kind, attributes=_body(request)
        )
        return _document_response(
            clinic_id=clinic_id, kind=kind, resource_id=resource.pk, status=201
        )
    if request.method == "GET":
        return auth_response(
            JsonResponse(
                search_resources(clinic_id=clinic_id, kind=kind),
                content_type="application/scim+json",
            )
        )
    return _error(405)


def _mutation_values(
    request: HttpRequest, *, kind: str, current: Mapping[str, object]
) -> dict[str, object]:
    if request.method == "PUT":
        body = _body(request)
        if body.get("id", current["id"]) != current["id"]:
            raise ScimInputError
        return {key: value for key, value in body.items() if key not in {"id", "meta"}}
    values = {
        key: value
        for key, value in current.items()
        if key not in {"id", "meta", "schemas"}
    }
    if request.method == "PATCH":
        body = _body(request)
        if set(body) != {"schemas", "Operations"} or body["schemas"] != [
            "urn:ietf:params:scim:api:messages:2.0:PatchOp"
        ]:
            raise ScimInputError
        return patched(kind=kind, current=values, operations=body["Operations"])
    return values


def _resource(
    request: HttpRequest, *, clinic_id: UUID, kind: str, resource_id: str
) -> HttpResponseBase:
    identifier = UUID(resource_id)
    if request.method == "GET":
        return _document_response(
            clinic_id=clinic_id, kind=kind, resource_id=identifier
        )
    current = resource_document(clinic_id=clinic_id, resource_id=identifier, kind=kind)
    if request.method not in {"PATCH", "PUT", "DELETE"}:
        return _error(405)
    update_resource(
        clinic_id=clinic_id,
        resource_id=identifier,
        values=_mutation_values(request, kind=kind, current=current),
        expected_revision=_expected_revision(request),
        delete=request.method == "DELETE",
    )
    if request.method == "DELETE":
        return auth_response(HttpResponse(status=204))
    return _document_response(clinic_id=clinic_id, kind=kind, resource_id=identifier)


@csrf_exempt
def scim_endpoint(
    request: HttpRequest,
    *,
    kind: str,
    resource_id: str | None = None,
    search: bool = False,
) -> HttpResponseBase:
    """Ignore staff cookies and keep all search terms in authenticated POST bodies."""
    header = request.headers.get("Authorization", "")
    if request.GET or not header.startswith("Bearer "):
        return _error(400 if request.GET else 401, "sensitive" if request.GET else "")
    authenticated = False
    try:
        with scim_context(bearer=header[7:]) as scope:
            authenticated = True
            if search:
                return _search(request, clinic_id=scope.clinic_id, kind=kind)
            return (
                _collection(request, clinic_id=scope.clinic_id, kind=kind)
                if resource_id is None
                else _resource(
                    request,
                    clinic_id=scope.clinic_id,
                    kind=kind,
                    resource_id=resource_id,
                )
            )
    except ScimRateLimitError:
        response = _error(429)
        response["Retry-After"] = "60"
        return response
    except ScimInputError as error:
        return _error(
            409 if isinstance(error, ScimConflictError) else 400, error.scim_type
        )
    except GovernanceDeniedError:
        return _error(404 if authenticated else 401)
