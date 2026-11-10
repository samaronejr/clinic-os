"""Bearer-token authentication, scope permission and per-client rate limiting.

A request's authority is only what ``interop_api_session`` resolves from the
presented token digest: an unexpired token of an active client whose approving
administrator still holds organization authority. No staff GUC is set; every
data resolver re-validates the same digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from django.contrib.auth.models import AnonymousUser
from django.db import connection
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission
from rest_framework.throttling import BaseThrottle

from apps.interop.api.errors import (
    BEARER_REALM,
    TEMPORARILY_UNAVAILABLE,
    ExternalApiError,
)
from apps.interop.credentials import access_token_digest
from apps.interop.ratelimit import (
    OUTAGE_RETRY_SECONDS,
    RateLimitUnavailableError,
    bucket,
)

if TYPE_CHECKING:
    from uuid import UUID

    from drf_spectacular.openapi import AutoSchema
    from rest_framework.request import Request
    from rest_framework.views import APIView


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    """The authenticated partner client of one request."""

    client_id: UUID
    organization_id: UUID
    clinic_id: UUID
    scopes: frozenset[str]
    rate_limit: int
    token_digest: str


def resolve_principal(token_digest: str) -> ApiPrincipal | None:
    """Resolve one token digest through the authenticating resolver."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.interop_api_session(%s)", [token_digest]
        )
        row = cursor.fetchone()
    if row is None:
        return None
    client_id, organization_id, clinic_id, scopes, rate_limit = row
    return ApiPrincipal(
        client_id,
        organization_id,
        clinic_id,
        frozenset(scopes),
        int(rate_limit),
        token_digest,
    )


class BearerTokenAuthentication(BaseAuthentication):
    """Accept only ``Authorization: Bearer at_<64 hex>`` of a live client."""

    def authenticate(self, request: Request) -> tuple[AnonymousUser, ApiPrincipal]:
        """Refuse any malformed, unknown, expired or revoked token identically."""
        header = request.META.get("HTTP_AUTHORIZATION", "")
        scheme, _, credential = header.partition(" ")
        digest = access_token_digest(credential) if scheme == "Bearer" else None
        principal = None if digest is None else resolve_principal(digest)
        if principal is None:
            raise AuthenticationFailed
        return AnonymousUser(), principal

    def authenticate_header(self, request: Request) -> str:
        """Advertise the bearer scheme on 401 responses."""
        del request
        return BEARER_REALM


class ScopePermission(BasePermission):
    """Require the view's single declared scope on the presented token."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Allow only a principal whose token carries the required scope."""
        principal = request.auth
        required = getattr(view, "required_scope", None)
        return (
            isinstance(principal, ApiPrincipal)
            and isinstance(required, str)
            and required in principal.scopes
        )


class ClientRateThrottle(BaseThrottle):
    """Spend one token of the authenticated client's bucket per request."""

    bucket_scope: ClassVar[str] = "api"

    def __init__(self) -> None:
        """Start with no pending wait."""
        self._wait = 0

    def allow_request(self, request: Request, view: APIView) -> bool:
        """Fail closed with 503 when the metering backend cannot decide."""
        del view
        principal = request.auth
        if not isinstance(principal, ApiPrincipal):
            return False
        try:
            decision = bucket().spend(
                client_id=principal.client_id,
                per_minute=principal.rate_limit,
                purpose=self.bucket_scope,
            )
        except RateLimitUnavailableError as error:
            raise ExternalApiError(
                TEMPORARILY_UNAVAILABLE, retry_after=OUTAGE_RETRY_SECONDS
            ) from error
        self._wait = decision.retry_after_seconds
        return decision.allowed

    def wait(self) -> float:
        """Return the whole seconds until the next token."""
        return float(self._wait)


class BearerScheme(OpenApiAuthenticationExtension):
    """Describe the bearer access token in the contract."""

    target_class = "apps.interop.api.authentication.BearerTokenAuthentication"
    name = "bearerToken"

    def get_security_definition(self, auto_schema: AutoSchema) -> dict[str, str]:
        """Return the HTTP bearer scheme definition."""
        del auto_schema
        return {"type": "http", "scheme": "bearer"}
