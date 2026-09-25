"""The ``{code, message_key}`` error contract of the internal UI API.

Every refusal carries only a stable machine code and an i18n message key; no
input, identifier, or reason detail is reflected. Unknown and foreign records
share the single ``access_denied`` body.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404, JsonResponse
from rest_framework import exceptions
from rest_framework.response import Response

if TYPE_CHECKING:
    from collections.abc import Mapping

UI_API_PREFIX: Final = "/api/ui/"


@dataclass(frozen=True, slots=True)
class ApiError:
    """One stable API refusal: HTTP status plus machine code."""

    status: int
    code: str

    @property
    def message_key(self) -> str:
        """Return the client-side i18n key for this refusal."""
        return f"api.error.{self.code}"

    def body(self) -> dict[str, str]:
        """Return the exact wire body."""
        return {"code": self.code, "message_key": self.message_key}


ACCESS_DENIED: Final = ApiError(403, "access_denied")
CSRF_FAILED: Final = ApiError(403, "csrf_failed")
STEP_UP_REQUIRED: Final = ApiError(403, "step_up_required")
MALFORMED_BODY: Final = ApiError(400, "malformed_body")
INVALID_INPUT: Final = ApiError(400, "invalid_input")
METHOD_NOT_ALLOWED: Final = ApiError(405, "method_not_allowed")
NOT_ACCEPTABLE: Final = ApiError(406, "not_acceptable")
UNSUPPORTED_MEDIA_TYPE: Final = ApiError(415, "unsupported_media_type")
THROTTLED: Final = ApiError(429, "throttled")
ERROR_CODES: Final = tuple(
    error.code
    for error in (
        ACCESS_DENIED,
        CSRF_FAILED,
        STEP_UP_REQUIRED,
        MALFORMED_BODY,
        INVALID_INPUT,
        METHOD_NOT_ALLOWED,
        NOT_ACCEPTABLE,
        UNSUPPORTED_MEDIA_TYPE,
        THROTTLED,
    )
)


class UiApiError(exceptions.APIException):
    """Raise one contract refusal from inside a UI API view."""

    def __init__(self, error: ApiError) -> None:
        """Bind the refusal; the detail never carries caller input."""
        super().__init__(detail=error.code, code=error.code)
        self.error = error


class CsrfFailedError(exceptions.PermissionDenied):
    """Session request without a valid ``X-CSRFToken`` header."""


class StepUpRequiredError(exceptions.PermissionDenied):
    """Privileged actor whose session has no confirmed TOTP verification."""


# Order matters: subclasses precede their DRF base classes.
_DRF_ERRORS: Final[tuple[tuple[type[exceptions.APIException], ApiError], ...]] = (
    (CsrfFailedError, CSRF_FAILED),
    (StepUpRequiredError, STEP_UP_REQUIRED),
    (exceptions.ParseError, MALFORMED_BODY),
    (exceptions.ValidationError, INVALID_INPUT),
    (exceptions.NotAuthenticated, ACCESS_DENIED),
    (exceptions.AuthenticationFailed, ACCESS_DENIED),
    (exceptions.PermissionDenied, ACCESS_DENIED),
    (exceptions.NotFound, ACCESS_DENIED),
    (exceptions.MethodNotAllowed, METHOD_NOT_ALLOWED),
    (exceptions.NotAcceptable, NOT_ACCEPTABLE),
    (exceptions.UnsupportedMediaType, UNSUPPORTED_MEDIA_TYPE),
    (exceptions.Throttled, THROTTLED),
)


def _contract_error(exc: Exception) -> ApiError | None:
    if isinstance(exc, UiApiError):
        return exc.error
    if isinstance(exc, (Http404, DjangoPermissionDenied)):
        return ACCESS_DENIED
    for exception_type, error in _DRF_ERRORS:
        if isinstance(exc, exception_type):
            return error
    return None


def ui_api_exception_handler(
    exc: Exception,
    context: Mapping[str, object],  # noqa: ARG001 - DRF handler signature
) -> Response | None:
    """Map DRF and view refusals to the contract; anything else is a 500."""
    error = _contract_error(exc)
    if error is None:
        return None
    return Response(error.body(), status=error.status)


def ui_api_denial_response() -> JsonResponse:
    """Return the contract denial for refusals raised before DRF runs."""
    return JsonResponse(ACCESS_DENIED.body(), status=ACCESS_DENIED.status)
