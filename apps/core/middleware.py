"""Outermost response privacy boundary for every product path."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from config.settings.contracts import LIVE_DATA_MODE
from django.conf import settings
from django.http import HttpResponse
from django.utils.cache import patch_cache_control, patch_vary_headers
from ops.release.activation import LiveModeHaltedError, require_live_runtime

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponseBase

PUBLIC_PREFIXES: Final = ("/static/",)
PRIVATE_VARY_HEADERS: Final = ("Cookie", "HX-Request")

# Infrastructure probes stay reachable while live mode is halted; every
# product path — reads included — stops serving once the activation is
# disabled or drifted, so a rollback cannot leave live data flowing.
LIVE_HALT_BYPASS_PATHS: Final = frozenset(
    {"/healthz", "/healthz/", "/readyz", "/readyz/"}
)
LIVE_HALT_BYPASS_PREFIXES: Final = ("/static/",)
SERVICE_UNAVAILABLE_STATUS: Final = 503


def is_private_product_path(path: str) -> bool:
    """Report whether one request path carries private clinical product state."""
    return not path.startswith(PUBLIC_PREFIXES)


class LiveModeHaltMiddleware:
    """Halt every product request when the live activation stops authorizing.

    ``ops.release.activation.disable`` flips the activation record; running
    processes re-validate it here on every request, so rollback stops new
    live work in already-running web workers without waiting for a
    restart. Outside live mode this middleware is a pass-through.
    """

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the downstream handler this boundary always wraps."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Refuse product requests once the live activation is not active."""
        if getattr(settings, "CLINIC_DATA_MODE", None) != LIVE_DATA_MODE:
            return self.get_response(request)
        path = request.path_info
        if path in LIVE_HALT_BYPASS_PATHS or path.startswith(LIVE_HALT_BYPASS_PREFIXES):
            return self.get_response(request)
        try:
            require_live_runtime(os.environ)
        except LiveModeHaltedError:
            return HttpResponse(status=SERVICE_UNAVAILABLE_STATUS)
        return self.get_response(request)


class ResponsePrivacyMiddleware:
    """Mark every product response private, unstored, and variant aware."""

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the downstream handler this boundary always wraps."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Apply the private cache contract to inner responses and refusals."""
        response = self.get_response(request)
        if is_private_product_path(request.path_info):
            patch_cache_control(
                response,
                no_cache=True,
                no_store=True,
                must_revalidate=True,
                private=True,
            )
            patch_vary_headers(response, PRIVATE_VARY_HEADERS)
        return response
