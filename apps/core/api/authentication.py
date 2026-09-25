"""Session + CSRF authentication and step-up permission for the UI API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from apps.core.api.errors import CsrfFailedError, StepUpRequiredError
from apps.identity.models import User
from apps.identity.otp import is_confirmed_verified_user, is_privileged_user

if TYPE_CHECKING:
    from drf_spectacular.openapi import AutoSchema
    from rest_framework.request import Request
    from rest_framework.views import APIView

CSRF_HEADER_NAME = "X-CSRFToken"


class UiSessionAuthentication(SessionAuthentication):
    """Django session auth whose CSRF refusal keeps its own contract code."""

    def enforce_csrf(self, request: Request) -> None:
        """Require the ``X-CSRFToken`` header on every session request."""
        try:
            super().enforce_csrf(request)
        except PermissionDenied as error:
            raise CsrfFailedError from error


class StepUpVerified(BasePermission):
    """Privileged roles need a confirmed TOTP session, as the HTML views do."""

    def has_permission(self, request: Request, view: APIView) -> bool:  # noqa: ARG002
        """Allow non-privileged actors; refuse unverified privileged ones."""
        user = request.user
        if not isinstance(user, User) or not is_privileged_user(user):
            return True
        if is_confirmed_verified_user(user):
            return True
        raise StepUpRequiredError


class UiSessionScheme(OpenApiAuthenticationExtension):
    """Describe the session cookie plus CSRF header pair in the contract."""

    target_class = "apps.core.api.authentication.UiSessionAuthentication"
    # Both halves of the credential; the generated requirement ANDs them.
    name = ["sessionCookie", "csrfHeader"]  # noqa: RUF012 - extension base attribute

    def get_security_definition(
        self,
        auto_schema: AutoSchema,  # noqa: ARG002 - extension signature
    ) -> list[dict[str, object]]:
        """Return the cookie and header API-key definitions (both required)."""
        return [
            {"type": "apiKey", "in": "cookie", "name": settings.SESSION_COOKIE_NAME},
            {"type": "apiKey", "in": "header", "name": CSRF_HEADER_NAME},
        ]
