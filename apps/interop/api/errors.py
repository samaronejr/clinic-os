"""The ``{code, message_key}`` error contract of the external API.

Refusals reflect no input. Unknown, foreign, malformed and out-of-scope record
selectors all produce the identical ``not_found`` response. Unexpected errors
become ``internal_error`` and are logged as type, route and frame locations only.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import PurePath
from typing import TYPE_CHECKING, Final

from django.http import Http404, HttpRequest, JsonResponse
from django.utils.log import log_response
from rest_framework import exceptions
from rest_framework.response import Response

if TYPE_CHECKING:
    from collections.abc import Mapping

EXTERNAL_API_PREFIX: Final = "/api/v1/"
BEARER_REALM: Final = 'Bearer realm="clinic-ops"'


@dataclass(frozen=True, slots=True)
class ExternalError:
    """One stable refusal: HTTP status plus machine code."""

    status: int
    code: str

    @property
    def message_key(self) -> str:
        """Return the client-side message key for this refusal."""
        return f"api.error.{self.code}"

    def body(self) -> dict[str, str]:
        """Return the exact wire body."""
        return {"code": self.code, "message_key": self.message_key}


INVALID_TOKEN: Final = ExternalError(401, "invalid_token")
INSUFFICIENT_SCOPE: Final = ExternalError(403, "insufficient_scope")
NOT_FOUND: Final = ExternalError(404, "not_found")
INVALID_INPUT: Final = ExternalError(400, "invalid_input")
MALFORMED_BODY: Final = ExternalError(400, "malformed_body")
IDEMPOTENCY_KEY_REQUIRED: Final = ExternalError(400, "idempotency_key_required")
IDEMPOTENCY_CONFLICT: Final = ExternalError(409, "idempotency_conflict")
SUBSCRIPTION_REVOKED: Final = ExternalError(409, "subscription_revoked")
METHOD_NOT_ALLOWED: Final = ExternalError(405, "method_not_allowed")
NOT_ACCEPTABLE: Final = ExternalError(406, "not_acceptable")
UNSUPPORTED_MEDIA_TYPE: Final = ExternalError(415, "unsupported_media_type")
RATE_LIMITED: Final = ExternalError(429, "rate_limited")
TEMPORARILY_UNAVAILABLE: Final = ExternalError(503, "temporarily_unavailable")
INTERNAL_ERROR: Final = ExternalError(500, "internal_error")
ERRORS: Final = (
    INVALID_TOKEN,
    INSUFFICIENT_SCOPE,
    NOT_FOUND,
    INVALID_INPUT,
    MALFORMED_BODY,
    IDEMPOTENCY_KEY_REQUIRED,
    IDEMPOTENCY_CONFLICT,
    SUBSCRIPTION_REVOKED,
    METHOD_NOT_ALLOWED,
    NOT_ACCEPTABLE,
    UNSUPPORTED_MEDIA_TYPE,
    RATE_LIMITED,
    TEMPORARILY_UNAVAILABLE,
    INTERNAL_ERROR,
)


class ExternalApiError(exceptions.APIException):
    """Raise one contract refusal, optionally with a ``Retry-After``."""

    def __init__(self, error: ExternalError, *, retry_after: int | None = None) -> None:
        """Bind the refusal; the detail never carries caller input."""
        super().__init__(detail=error.code, code=error.code)
        self.error = error
        self.retry_after = retry_after


_DRF_ERRORS: Final[tuple[tuple[type[exceptions.APIException], ExternalError], ...]] = (
    (exceptions.NotAuthenticated, INVALID_TOKEN),
    (exceptions.AuthenticationFailed, INVALID_TOKEN),
    (exceptions.PermissionDenied, INSUFFICIENT_SCOPE),
    (exceptions.NotFound, NOT_FOUND),
    (exceptions.ParseError, MALFORMED_BODY),
    (exceptions.ValidationError, INVALID_INPUT),
    (exceptions.MethodNotAllowed, METHOD_NOT_ALLOWED),
    (exceptions.NotAcceptable, NOT_ACCEPTABLE),
    (exceptions.UnsupportedMediaType, UNSUPPORTED_MEDIA_TYPE),
    (exceptions.Throttled, RATE_LIMITED),
)


def error_response(error: ExternalError, *, retry_after: int | None = None) -> Response:
    """Build one refusal with its exact authentication or retry headers."""
    response = Response(error.body(), status=error.status)
    if error is INVALID_TOKEN:
        response["WWW-Authenticate"] = f'{BEARER_REALM}, error="invalid_token"'
    elif error is INSUFFICIENT_SCOPE:
        response["WWW-Authenticate"] = f'{BEARER_REALM}, error="insufficient_scope"'
    if retry_after is not None:
        response["Retry-After"] = str(retry_after)
    return response


def _frame_locations(exc: Exception) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    return " < ".join(
        f"{PurePath(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in reversed(frames)
    )


def external_api_exception_handler(
    exc: Exception, context: Mapping[str, object]
) -> Response:
    """Map every refusal to the contract; anything unexpected is a JSON 500."""
    if isinstance(exc, ExternalApiError):
        return error_response(exc.error, retry_after=exc.retry_after)
    if isinstance(exc, Http404):
        return error_response(NOT_FOUND)
    for exception_type, error in _DRF_ERRORS:
        if isinstance(exc, exception_type):
            wait = getattr(exc, "wait", None) if error is RATE_LIMITED else None
            retry = None if wait is None else max(1, int(-(-float(wait) // 1)))
            return error_response(error, retry_after=retry)
    response = error_response(INTERNAL_ERROR)
    request = context["request"]
    django_request = getattr(request, "_request", request)
    if not isinstance(django_request, HttpRequest):
        raise exc
    match = django_request.resolver_match
    # The shared allowlisted template: route name, type and frames, never a message.
    log_response(
        "UI API internal error: route=%s exception=%s frames=%s",
        match.view_name if match is not None else "unresolved",
        f"{type(exc).__module__}.{type(exc).__qualname__}",
        _frame_locations(exc),
        response=response,
        request=django_request,
    )
    return response


def not_found_response() -> JsonResponse:
    """Return the contract body for any path outside the published API."""
    return JsonResponse(NOT_FOUND.body(), status=NOT_FOUND.status)
