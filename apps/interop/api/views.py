"""External API v1 views: OAuth token issuance, scoped reads and idempotent writes.

Each view adapts one resolver call. Path identifiers are opaque external ids;
unknown, foreign, malformed and other-clinic ids are the same ``not_found``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar, Final, cast
from uuid import UUID, uuid4

import rfc8785
from django.db import DatabaseError, connection
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import exceptions, serializers
from rest_framework.parsers import FormParser, JSONParser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.secrets import SecretUnavailableError
from apps.interop.api.authentication import (
    ApiPrincipal,
    BearerTokenAuthentication,
    ClientRateThrottle,
    ScopePermission,
)
from apps.interop.api.errors import (
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_KEY_REQUIRED,
    INVALID_INPUT,
    INVALID_TOKEN,
    NOT_FOUND,
    SUBSCRIPTION_REVOKED,
    TEMPORARILY_UNAVAILABLE,
    ExternalApiError,
    external_api_exception_handler,
    not_found_response,
)
from apps.interop.api.serializers import (
    AppointmentSerializer,
    AvailabilityEntrySerializer,
    ErrorSerializer,
    InvoiceSerializer,
    OAuthErrorSerializer,
    PatientSerializer,
    TokenRequestSerializer,
    TokenResponseSerializer,
    WebhookCreateSerializer,
    WebhookSecretSerializer,
    WebhookSubscriptionSerializer,
    WindowQuerySerializer,
)
from apps.interop.credentials import (
    TOKEN_LIFETIME_SECONDS,
    access_token_digest,
    client_secret_digest,
    new_access_token,
)
from apps.interop.external_ids import Kind, encoder, internal_id
from apps.interop.models import SCOPES
from apps.interop.ratelimit import (
    OUTAGE_RETRY_SECONDS,
    TOKEN_REQUESTS_PER_MINUTE,
    RateLimitUnavailableError,
    bucket,
)
from apps.interop.webhooks import JsonValue, public_status, signing_secret
from apps.tenancy.envelope import _kek

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from django.http import HttpRequest, JsonResponse
    from rest_framework.request import Request

    type ExceptionHandler = Callable[[Exception, Mapping[str, object]], Response | None]

DEFAULT_PAGE: Final = 50
MAX_WINDOW: Final = timedelta(days=31)
MAX_AVAILABILITY: Final = 1000
MAX_SCOPE_CHARS: Final = 200
IDEMPOTENCY_KEY: Final = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_LABEL: Final = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME: Final = re.compile(rf"(?=.{{4,253}}$){_LABEL}(?:\.{_LABEL})+")
_URL_PATH: Final = re.compile(r"(/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?")
_PORT: Final = re.compile(r"[0-9]{1,5}")
MAX_PORT: Final = 65535
_WRITE_ERRORS: Final = {
    "not_found": NOT_FOUND,
    "invalid_input": INVALID_INPUT,
    "idempotency_conflict": IDEMPOTENCY_CONFLICT,
    "subscription_revoked": SUBSCRIPTION_REVOKED,
}
ERRORS: Final = {
    400: OpenApiResponse(ErrorSerializer, description="Invalid input."),
    401: OpenApiResponse(
        ErrorSerializer, description="Missing, expired or revoked bearer token."
    ),
    403: OpenApiResponse(
        ErrorSerializer, description="The token lacks this resource's scope."
    ),
    429: OpenApiResponse(
        ErrorSerializer, description="The client's token bucket is empty."
    ),
    503: OpenApiResponse(
        ErrorSerializer, description="Rate metering is unavailable; nothing ran."
    ),
}
READ_ERRORS: Final = {
    **ERRORS,
    404: OpenApiResponse(
        ErrorSerializer, description="Unknown, foreign and malformed ids alike."
    ),
}
WRITE_ERRORS: Final = {
    **READ_ERRORS,
    409: OpenApiResponse(
        ErrorSerializer,
        description="Idempotency-Key reused for another request, or revoked target.",
    ),
}
RETRY_AFTER: Final = OpenApiParameter(
    "Retry-After",
    int,
    OpenApiParameter.HEADER,
    response=[429, 503],
    description="Seconds to wait before the next attempt.",
)
IDEMPOTENCY_HEADER: Final = OpenApiParameter(
    "Idempotency-Key",
    str,
    OpenApiParameter.HEADER,
    required=True,
    description="1-64 characters of [A-Za-z0-9._:-]; replays return the stored result.",
)
WINDOW_PARAMETERS: Final = [
    OpenApiParameter("start", str, required=True, description="ISO 8601 with offset."),
    OpenApiParameter("end", str, required=True, description="ISO 8601 with offset."),
    OpenApiParameter("limit", int, description="1-100, default 50."),
    OpenApiParameter("cursor", str, description="The previous page's next_cursor."),
]
OPAQUE_ID: Final = "An opaque external id returned by this API."
APPOINTMENT_PAGE: Final = inline_serializer(
    name="AppointmentPage",
    fields={
        "data": AppointmentSerializer(many=True),
        "next_cursor": serializers.CharField(allow_null=True),
    },
)
INVOICE_PAGE: Final = inline_serializer(
    name="InvoicePage",
    fields={
        "data": InvoiceSerializer(many=True),
        "next_cursor": serializers.CharField(allow_null=True),
    },
)
AVAILABILITY: Final = inline_serializer(
    name="Availability",
    fields={
        "data": AvailabilityEntrySerializer(many=True),
        "truncated": serializers.BooleanField(),
    },
)
WEBHOOK_LIST: Final = inline_serializer(
    name="WebhookSubscriptionList",
    fields={"data": WebhookSubscriptionSerializer(many=True)},
)


def _z(value: object) -> str | None:
    """Normalize a resolver timestamp to UTC ``Z`` notation."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExternalApiError(INVALID_INPUT)
    return (
        datetime.fromisoformat(value).astimezone(UTC).isoformat().replace("+00:00", "Z")
    )


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ExternalApiError(INVALID_INPUT) from None
    if parsed.tzinfo is None:
        raise ExternalApiError(INVALID_INPUT)
    return parsed.astimezone(UTC)


def _uuid(value: object) -> UUID | None:
    return UUID(value) if isinstance(value, str) else None


def _resolver_rows(  # noqa: PLR0913 - the reader's exact bounded terms
    principal: ApiPrincipal,
    resource: str,
    *,
    selector: UUID | None = None,
    window: tuple[datetime, datetime] | None = None,
    after: tuple[datetime, UUID] | None = None,
    page_size: int = 1,
) -> list[dict[str, object]]:
    """Run the authenticating reader; its own scope check repeats the view's."""
    try:
        kek = _kek()
    except SecretUnavailableError as error:
        raise ExternalApiError(
            TEMPORARILY_UNAVAILABLE, retry_after=OUTAGE_RETRY_SECONDS
        ) from error
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.interop_api_read(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [
                    principal.token_digest,
                    kek if resource == "patient" else "",
                    resource,
                    selector,
                    None if window is None else window[0],
                    None if window is None else window[1],
                    None if after is None else after[0],
                    None if after is None else after[1],
                    page_size,
                ],
            )
            rows = cursor.fetchall()
    except DatabaseError as error:
        sqlstate = getattr(error.__cause__, "sqlstate", None)
        if sqlstate == "42501":
            raise ExternalApiError(INVALID_TOKEN) from None
        if sqlstate == "22023":
            raise ExternalApiError(INVALID_INPUT) from None
        raise
    return [
        cast(
            "dict[str, object]",
            json.loads(row[0]) if isinstance(row[0], str) else row[0],
        )
        for row in rows
    ]


def _encode(principal: ApiPrincipal) -> Callable[[Kind, UUID | None], str | None]:
    try:
        return encoder(principal.organization_id)
    except SecretUnavailableError as error:
        raise ExternalApiError(
            TEMPORARILY_UNAVAILABLE, retry_after=OUTAGE_RETRY_SECONDS
        ) from error


def _selector(principal: ApiPrincipal, kind: Kind, value: str) -> UUID:
    try:
        selected = internal_id(kind, principal.organization_id, value)
    except SecretUnavailableError as error:
        raise ExternalApiError(
            TEMPORARILY_UNAVAILABLE, retry_after=OUTAGE_RETRY_SECONDS
        ) from error
    if selected is None:
        raise ExternalApiError(NOT_FOUND)
    return selected


def _window(request: Request) -> tuple[tuple[datetime, datetime], int, str | None]:
    query = WindowQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    data = query.validated_data
    start, end = _instant(data["start"]), _instant(data["end"])
    if not start < end <= start + MAX_WINDOW:
        raise ExternalApiError(INVALID_INPUT)
    return (start, end), int(data.get("limit", DEFAULT_PAGE)), data.get("cursor")


def _cursor(
    principal: ApiPrincipal, kind: Kind, value: str | None
) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ExternalApiError(INVALID_INPUT) from None
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"a", "s"}
        or not isinstance(decoded["s"], str)
    ):
        raise ExternalApiError(INVALID_INPUT)
    selected = internal_id(kind, principal.organization_id, decoded["a"])
    if selected is None:
        raise ExternalApiError(INVALID_INPUT)
    return _instant(decoded["s"]), selected


def _next_cursor(external: str | None, instant: str | None) -> str:
    raw = rfc8785.dumps({"a": external, "s": instant})
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _appointment(
    item: Mapping[str, object], encode: Callable[[Kind, UUID | None], str | None]
) -> dict[str, object]:
    return {
        "id": encode("appointment", _uuid(item["id"])),
        "status": public_status(str(item["status"])),
        "start_at": _z(item["start_at"]),
        "end_at": _z(item["end_at"]),
        "patient": encode("patient", _uuid(item["patient"])),
        "practitioner": encode("practitioner", _uuid(item["practitioner"])),
        "service_type": encode("service", _uuid(item["service_type"])),
        "created_at": _z(item["created_at"]),
        "updated_at": _z(item["updated_at"]),
    }


def _invoice(
    item: Mapping[str, object], encode: Callable[[Kind, UUID | None], str | None]
) -> dict[str, object]:
    return {
        "id": encode("invoice", _uuid(item["id"])),
        "status": str(item["status"]),
        "amount_minor": item["amount_minor"],
        "currency": item["currency"],
        "appointment": encode("appointment", _uuid(item["appointment"])),
        "patient": encode("patient", _uuid(item["patient"])),
        "issued_at": _z(item["issued_at"]),
    }


def _webhook(item: Mapping[str, object], *, with_secret: bool) -> dict[str, object]:
    subscription = UUID(str(item["id"]))
    version = int(cast("int", item["secret_version"]))
    shown: dict[str, object] = {
        "id": str(subscription),
        "url": item.get("url"),
        "events": item.get("events"),
        "state": item["state"],
        "secret_version": version,
        "created_at": _z(item.get("created_at")),
        "revoked_at": _z(item.get("revoked_at")),
    }
    if with_secret:
        try:
            shown["secret"] = signing_secret(subscription, version)
        except SecretUnavailableError as error:
            raise ExternalApiError(
                TEMPORARILY_UNAVAILABLE, retry_after=OUTAGE_RETRY_SECONDS
            ) from error
    return shown


class ExternalApiView(APIView):
    """Base of every bearer-authenticated, scoped, rate-limited endpoint."""

    authentication_classes = (BearerTokenAuthentication,)
    permission_classes = (ScopePermission,)
    throttle_classes = (ClientRateThrottle,)
    parser_classes = (JSONParser,)
    renderer_classes = (JSONRenderer,)
    required_scope: ClassVar[str] = ""

    def get_exception_handler(self) -> ExceptionHandler:
        """Route every refusal through the external contract."""
        return external_api_exception_handler

    @staticmethod
    def principal(request: Request) -> ApiPrincipal:
        """Return the principal the authentication class resolved."""
        principal = request.auth
        if not isinstance(principal, ApiPrincipal):
            raise exceptions.NotAuthenticated
        return principal


class AppointmentListView(ExternalApiView):
    """List the client clinic's appointments starting inside a window."""

    required_scope = "appointments:read"

    @extend_schema(
        operation_id="appointments_list",
        tags=["appointments"],
        parameters=[*WINDOW_PARAMETERS, RETRY_AFTER],
        responses={200: APPOINTMENT_PAGE, **ERRORS},
    )
    def get(self, request: Request) -> Response:
        """Return one keyset page ordered by start time and id."""
        principal = self.principal(request)
        window, limit, cursor = _window(request)
        rows = _resolver_rows(
            principal,
            "appointments",
            window=window,
            after=_cursor(principal, "appointment", cursor),
            page_size=limit + 1,
        )
        encode = _encode(principal)
        data = [_appointment(row, encode) for row in rows[:limit]]
        last = data[-1] if len(rows) > limit else None
        return Response(
            {
                "data": data,
                "next_cursor": None
                if last is None
                else _next_cursor(
                    cast("str", last["id"]), cast("str", last["start_at"])
                ),
            }
        )


class AppointmentDetailView(ExternalApiView):
    """Read one appointment by external id."""

    required_scope = "appointments:read"

    @extend_schema(
        operation_id="appointments_retrieve",
        tags=["appointments"],
        parameters=[
            OpenApiParameter(
                "appointment_id", str, OpenApiParameter.PATH, description=OPAQUE_ID
            ),
            RETRY_AFTER,
        ],
        responses={200: AppointmentSerializer, **READ_ERRORS},
    )
    def get(self, request: Request, appointment_id: str) -> Response:
        """Unknown, foreign and malformed ids are the same 404."""
        principal = self.principal(request)
        rows = _resolver_rows(
            principal,
            "appointment",
            selector=_selector(principal, "appointment", appointment_id),
        )
        if not rows:
            raise ExternalApiError(NOT_FOUND)
        return Response(_appointment(rows[0], _encode(principal)))


class PatientDetailView(ExternalApiView):
    """Read the minimal demographics of one patient of the client clinic."""

    required_scope = "patients.demographics:read"

    @extend_schema(
        operation_id="patients_retrieve",
        tags=["patients"],
        parameters=[
            OpenApiParameter(
                "patient_id", str, OpenApiParameter.PATH, description=OPAQUE_ID
            ),
            RETRY_AFTER,
        ],
        responses={200: PatientSerializer, **READ_ERRORS},
    )
    def get(self, request: Request, patient_id: str) -> Response:
        """Return only the display name and birth date; no other field exists."""
        principal = self.principal(request)
        rows = _resolver_rows(
            principal,
            "patient",
            selector=_selector(principal, "patient", patient_id),
        )
        if not rows:
            raise ExternalApiError(NOT_FOUND)
        row = rows[0]
        return Response(
            {
                "id": patient_id,
                "display_name": row["display_name"],
                "birth_date": row["birth_date"],
            }
        )


class AvailabilityView(ExternalApiView):
    """Working windows, busy intervals and closures overlapping a window."""

    required_scope = "availability:read"

    @extend_schema(
        operation_id="availability_list",
        tags=["availability"],
        parameters=[*WINDOW_PARAMETERS[:2], RETRY_AFTER],
        responses={200: AVAILABILITY, **ERRORS},
    )
    def get(self, request: Request) -> Response:
        """Return at most 1000 entries ordered by start time."""
        principal = self.principal(request)
        window, _limit, _cursor_value = _window(request)
        rows = _resolver_rows(
            principal, "availability", window=window, page_size=MAX_AVAILABILITY + 1
        )
        encode = _encode(principal)
        return Response(
            {
                "data": [
                    {
                        "kind": row["kind"],
                        "practitioner": encode(
                            "practitioner", _uuid(row["practitioner"])
                        ),
                        "start_at": _z(row["start_at"]),
                        "end_at": _z(row["end_at"]),
                    }
                    for row in rows[:MAX_AVAILABILITY]
                ],
                "truncated": len(rows) > MAX_AVAILABILITY,
            }
        )


class InvoiceListView(ExternalApiView):
    """List issued invoices of the client clinic by issue time."""

    required_scope = "invoices.status:read"

    @extend_schema(
        operation_id="invoices_list",
        tags=["invoices"],
        parameters=[*WINDOW_PARAMETERS, RETRY_AFTER],
        responses={200: INVOICE_PAGE, **ERRORS},
    )
    def get(self, request: Request) -> Response:
        """Return one keyset page ordered by issue time and id."""
        principal = self.principal(request)
        window, limit, cursor = _window(request)
        rows = _resolver_rows(
            principal,
            "invoices",
            window=window,
            after=_cursor(principal, "invoice", cursor),
            page_size=limit + 1,
        )
        encode = _encode(principal)
        data = [_invoice(row, encode) for row in rows[:limit]]
        last = data[-1] if len(rows) > limit else None
        return Response(
            {
                "data": data,
                "next_cursor": None
                if last is None
                else _next_cursor(
                    cast("str", last["id"]), cast("str", last["issued_at"])
                ),
            }
        )


class InvoiceDetailView(ExternalApiView):
    """Read one issued invoice's status by external id."""

    required_scope = "invoices.status:read"

    @extend_schema(
        operation_id="invoices_retrieve",
        tags=["invoices"],
        parameters=[
            OpenApiParameter(
                "invoice_id", str, OpenApiParameter.PATH, description=OPAQUE_ID
            ),
            RETRY_AFTER,
        ],
        responses={200: InvoiceSerializer, **READ_ERRORS},
    )
    def get(self, request: Request, invoice_id: str) -> Response:
        """Drafts are never visible; unknown and foreign ids are the same 404."""
        principal = self.principal(request)
        rows = _resolver_rows(
            principal,
            "invoice",
            selector=_selector(principal, "invoice", invoice_id),
        )
        if not rows:
            raise ExternalApiError(NOT_FOUND)
        return Response(_invoice(rows[0], _encode(principal)))


def _endpoint(value: str) -> str:
    """Accept only ``https://<dns-name>[:port][/path]``; no query or credentials."""
    scheme, separator, rest = value.partition("://")
    authority, slash, path = rest.partition("/")
    host, colon, port = authority.partition(":")
    if (
        scheme != "https"
        or not separator
        or _HOSTNAME.fullmatch(host) is None
        or host.replace(".", "").isdigit()
        or (colon and (_PORT.fullmatch(port) is None or not 1 <= int(port) <= MAX_PORT))
        or _URL_PATH.fullmatch(slash + path) is None
    ):
        raise ExternalApiError(INVALID_INPUT)
    return value


def _write(
    request: Request,
    principal: ApiPrincipal,
    operation: str,
    selector: UUID | None,
    body: dict[str, JsonValue],
) -> Response:
    """Run one idempotent write; a replay returns the stored response."""
    key = request.headers.get("Idempotency-Key")
    if key is None:
        raise ExternalApiError(IDEMPOTENCY_KEY_REQUIRED)
    if IDEMPOTENCY_KEY.fullmatch(key) is None:
        raise ExternalApiError(INVALID_INPUT)
    digest = hashlib.sha256(
        rfc8785.dumps(
            {
                "operation": operation,
                "selector": None if selector is None else str(selector),
                "body": body,
            }
        )
    ).hexdigest()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.interop_api_write(%s,%s,%s,%s,%s,%s::jsonb)",
                [
                    principal.token_digest,
                    operation,
                    key,
                    digest,
                    uuid4() if selector is None else selector,
                    rfc8785.dumps(body).decode("utf-8"),
                ],
            )
            row = cursor.fetchone()
    except DatabaseError as error:
        sqlstate = getattr(error.__cause__, "sqlstate", None)
        if sqlstate == "42501":
            raise ExternalApiError(INVALID_TOKEN) from None
        if sqlstate == "22023":
            raise ExternalApiError(INVALID_INPUT) from None
        raise
    if row is None:  # pragma: no cover - the writer always answers one row
        raise ExternalApiError(INVALID_INPUT)
    status, stored, replayed = row
    result = cast(
        "dict[str, object]", json.loads(stored) if isinstance(stored, str) else stored
    )
    if status >= 400:  # noqa: PLR2004 - HTTP error class
        raise ExternalApiError(
            _WRITE_ERRORS.get(str(result.get("code")), INVALID_INPUT)
        )
    response = Response(
        _webhook(result, with_secret=operation in {"webhook_create", "webhook_rotate"}),
        status=status,
    )
    if replayed:
        response["Idempotent-Replayed"] = "true"
    return response


class WebhookCollectionView(ExternalApiView):
    """List or register the calling client's webhook endpoints."""

    required_scope = "webhooks:manage"

    @extend_schema(
        operation_id="webhook_subscriptions_list",
        tags=["webhooks"],
        parameters=[RETRY_AFTER],
        responses={200: WEBHOOK_LIST, **ERRORS},
    )
    def get(self, request: Request) -> Response:
        """Return every registration of this client; secrets are never listed."""
        principal = self.principal(request)
        rows = _resolver_rows(principal, "webhooks", page_size=50)
        return Response({"data": [_webhook(row, with_secret=False) for row in rows]})

    @extend_schema(
        operation_id="webhook_subscriptions_create",
        tags=["webhooks"],
        parameters=[IDEMPOTENCY_HEADER, RETRY_AFTER],
        request=WebhookCreateSerializer,
        responses={201: WebhookSecretSerializer, **WRITE_ERRORS},
    )
    def post(self, request: Request) -> Response:
        """Register an endpoint; the signing secret is in the response."""
        principal = self.principal(request)
        payload = WebhookCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        events = payload.validated_data["events"]
        if len(set(events)) != len(events):
            raise ExternalApiError(INVALID_INPUT)
        return _write(
            request,
            principal,
            "webhook_create",
            None,
            {
                "url": _endpoint(payload.validated_data["url"]),
                "events": sorted(events),
            },
        )


class WebhookRevokeView(ExternalApiView):
    """Revoke one of the calling client's endpoints."""

    required_scope = "webhooks:manage"

    @extend_schema(
        operation_id="webhook_subscriptions_revoke",
        tags=["webhooks"],
        parameters=[
            OpenApiParameter("subscription_id", UUID, OpenApiParameter.PATH),
            IDEMPOTENCY_HEADER,
            RETRY_AFTER,
        ],
        request=None,
        responses={200: WebhookSubscriptionSerializer, **WRITE_ERRORS},
    )
    def post(self, request: Request, subscription_id: UUID) -> Response:
        """Stop deliveries; queued deliveries cancel at send time."""
        return _write(
            request, self.principal(request), "webhook_revoke", subscription_id, {}
        )


class WebhookRotateView(ExternalApiView):
    """Rotate one endpoint's signing secret."""

    required_scope = "webhooks:manage"

    @extend_schema(
        operation_id="webhook_subscriptions_rotate_secret",
        tags=["webhooks"],
        parameters=[
            OpenApiParameter("subscription_id", UUID, OpenApiParameter.PATH),
            IDEMPOTENCY_HEADER,
            RETRY_AFTER,
        ],
        request=None,
        responses={200: WebhookSecretSerializer, **WRITE_ERRORS},
    )
    def post(self, request: Request, subscription_id: UUID) -> Response:
        """Sign with both secrets for 24 hours, then with the new one only."""
        return _write(
            request, self.principal(request), "webhook_rotate", subscription_id, {}
        )


def _oauth_error(
    error: str, status: int, *, retry_after: int | None = None
) -> Response:
    response = Response({"error": error}, status=status)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    if status == 401:  # noqa: PLR2004 - RFC 6749 section 5.2
        response["WWW-Authenticate"] = 'Basic realm="clinic-ops"'
    if retry_after is not None:
        response["Retry-After"] = str(retry_after)
    return response


def _oauth_exception_handler(exc: Exception, context: Mapping[str, object]) -> Response:
    """Every refusal of the token endpoint keeps the RFC 6749 body."""
    if isinstance(exc, exceptions.APIException):
        return _oauth_error("invalid_request", 400)
    return external_api_exception_handler(exc, context)


def _client_credentials(request: Request) -> tuple[str, str] | None:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    form_id = request.data.get("client_id")
    form_secret = request.data.get("client_secret")
    if header:
        scheme, _, encoded = header.partition(" ")
        if scheme != "Basic" or form_id is not None or form_secret is not None:
            return None
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("ascii")
        except (binascii.Error, UnicodeDecodeError):
            return None
        client, separator, secret = decoded.partition(":")
        return (client, secret) if separator else None
    if isinstance(form_id, str) and isinstance(form_secret, str):
        return form_id, form_secret
    return None


class _TokenSchema(AutoSchema):
    """Document the HTTP Basic client authentication of the token endpoint."""

    def get_auth(self) -> list[dict[str, object]]:
        """Return the client_secret_basic requirement (form credentials also work)."""
        return [{"clientBasic": []}]


class TokenView(APIView):
    """OAuth 2.0 client credentials grant (RFC 6749 section 4.4)."""

    schema = _TokenSchema()
    authentication_classes = ()
    permission_classes = ()
    throttle_classes = ()
    parser_classes = (FormParser,)
    renderer_classes = (JSONRenderer,)

    def get_exception_handler(self) -> ExceptionHandler:
        """Keep RFC 6749 error bodies for every token-endpoint refusal."""
        return _oauth_exception_handler

    @extend_schema(
        operation_id="oauth_token",
        tags=["oauth"],
        parameters=[RETRY_AFTER],
        request={"application/x-www-form-urlencoded": TokenRequestSerializer},
        responses={
            200: TokenResponseSerializer,
            400: OpenApiResponse(
                OAuthErrorSerializer, description="invalid_request or invalid_scope."
            ),
            401: OpenApiResponse(
                OAuthErrorSerializer, description="invalid_client for any refusal."
            ),
            429: OpenApiResponse(
                OAuthErrorSerializer, description="Token requests are metered too."
            ),
            503: OpenApiResponse(
                OAuthErrorSerializer, description="Rate metering is unavailable."
            ),
        },
    )
    def post(self, request: Request) -> Response:
        """Issue a 15-minute bearer token for an active, authorized client."""
        terms = _token_terms(request)
        if isinstance(terms, Response):
            return terms
        client, secret, scopes = terms
        refused = _token_admission(client)
        if refused is not None:
            return refused
        if secret is None:
            return _oauth_error("invalid_client", 401)
        return _issue(client, secret, scopes)


def _requested_scopes(request: Request) -> list[str] | Response:
    """Return the sorted requested scopes, or the RFC 6749 refusal."""
    requested = request.data.get("scope", "")
    if not isinstance(requested, str) or len(requested) > MAX_SCOPE_CHARS:
        return _oauth_error("invalid_request", 400)
    scopes = sorted(set(requested.split()))
    if any(scope not in SCOPES for scope in scopes):
        return _oauth_error("invalid_scope", 400)
    return scopes


def _token_terms(request: Request) -> tuple[UUID, str | None, list[str]] | Response:
    """Validate the grant, client credentials and requested scope vocabulary."""
    grant = request.data.get("grant_type")
    if grant != "client_credentials":
        return _oauth_error(
            "unsupported_grant_type" if isinstance(grant, str) else "invalid_request",
            400,
        )
    credentials = _client_credentials(request)
    try:
        client = UUID(credentials[0]) if credentials is not None else None
    except ValueError:
        client = None
    if client is None or credentials is None:
        return _oauth_error("invalid_client", 401)
    scopes = _requested_scopes(request)
    if isinstance(scopes, Response):
        return scopes
    return client, client_secret_digest(credentials[1]), scopes


def _token_admission(client: UUID) -> Response | None:
    """Meter token requests per presented client id, before secret checks."""
    try:
        decision = bucket().spend(
            client_id=client, per_minute=TOKEN_REQUESTS_PER_MINUTE, purpose="token"
        )
    except RateLimitUnavailableError:
        return _oauth_error(
            "temporarily_unavailable", 503, retry_after=OUTAGE_RETRY_SECONDS
        )
    if not decision.allowed:
        return _oauth_error(
            "temporarily_unavailable", 429, retry_after=decision.retry_after_seconds
        )
    return None


def _issue(client: UUID, secret: str, scopes: list[str]) -> Response:
    """Store only the new token's digest; the token itself is returned once."""
    token = new_access_token()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.interop_api_token(%s,%s,%s::varchar[],%s)",
            [client, secret, scopes or None, access_token_digest(token)],
        )
        row = cursor.fetchone()
    if row is None:
        return _oauth_error("invalid_client", 401)
    outcome, granted, _expires_at = row
    if outcome != "issued":
        return _oauth_error("invalid_scope", 400)
    response = Response(
        {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": TOKEN_LIFETIME_SECONDS,
            "scope": " ".join(granted),
        }
    )
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


@csrf_exempt
def not_found(request: HttpRequest) -> JsonResponse:  # noqa: ARG001 - Django view
    """Answer any unpublished ``/api/v1/`` path with the JSON 404 body."""
    return not_found_response()
