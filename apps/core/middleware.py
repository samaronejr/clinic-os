"""Outermost response privacy boundary for every product path."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from config.settings.contracts import LIVE_DATA_MODE
from django.conf import settings
from django.http import HttpResponse
from django.utils.cache import patch_cache_control, patch_vary_headers
from ops.release.activation import LiveModeHaltedError, require_live_runtime

from apps.realtime.transport import publish_on_commit

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

CSP_HEADER: Final = "Content-Security-Policy"
CSP_REPORT_ONLY_HEADER: Final = "Content-Security-Policy-Report-Only"
# The strict first-party policy. No 'unsafe-inline', no 'unsafe-eval', no
# nonces: every script and style ships as a same-origin static file.
CSP_DIRECTIVES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("default-src", ("'self'",)),
    ("script-src", ("'self'",)),
    ("style-src", ("'self'",)),
    ("img-src", ("'self'", "data:", "blob:")),
    ("media-src", ("'self'", "blob:")),
    ("connect-src", ("'self'",)),
    ("worker-src", ("'self'",)),
    ("object-src", ("'none'",)),
    ("base-uri", ("'none'",)),
    ("frame-ancestors", ("'none'",)),
    ("form-action", ("'self'",)),
)
# Only these directives may gain origins, and only exact provider origins:
# script/style/object/base/frame-ancestors/form-action never widen.
CSP_EXTENSIBLE_DIRECTIVES: Final = frozenset({"connect-src", "img-src", "media-src"})
_CSP_ORIGIN: Final = re.compile(
    r"(?:https|wss)://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?::[0-9]{1,5})?"
)
_CSP_PATH_PREFIX: Final = re.compile(r"/(?:[a-z0-9-]+/)+")


def is_private_product_path(path: str) -> bool:
    """Report whether one request path carries private clinical product state."""
    return not path.startswith(PUBLIC_PREFIXES)


class CspExtensionError(ValueError):
    """Reject a per-route CSP extension that would weaken the strict policy."""


@dataclass(frozen=True, slots=True)
class CspExtension:
    """Extra origins for one directive on one route prefix while active.

    ``is_active`` is evaluated on every response, so an extension disappears
    as soon as its provider stops being activated; an exception from it
    propagates instead of guessing.
    """

    path_prefix: str
    directive: str
    origins: tuple[str, ...]
    is_active: Callable[[], bool]


# Per-route extension registry. Teleconsult provider origins register here
# from the provider lifecycle registry (todo 4) once a provider is activated;
# until then the registry stays empty and every route gets the base policy.
_CSP_EXTENSIONS: list[CspExtension] = []


def register_csp_extension(extension: CspExtension) -> None:
    """Validate and register one per-route CSP extension."""
    if _CSP_PATH_PREFIX.fullmatch(extension.path_prefix) is None:
        message = "CSP extension path prefix must be a lowercase /segment/ path"
        raise CspExtensionError(message)
    if extension.directive not in CSP_EXTENSIBLE_DIRECTIVES:
        message = "CSP extension directive is not extensible"
        raise CspExtensionError(message)
    if not extension.origins or any(
        _CSP_ORIGIN.fullmatch(origin) is None for origin in extension.origins
    ):
        message = "CSP extension origins must be exact https or wss origins"
        raise CspExtensionError(message)
    _CSP_EXTENSIONS.append(extension)


def content_security_policy(path: str) -> str:
    """Build the policy for one request path from base plus active extensions."""
    extra: dict[str, list[str]] = {}
    for extension in _CSP_EXTENSIONS:
        if path.startswith(extension.path_prefix) and extension.is_active():
            extra.setdefault(extension.directive, []).extend(extension.origins)
    directives = []
    for name, sources in CSP_DIRECTIVES:
        merged = list(sources)
        merged.extend(o for o in dict.fromkeys(extra.get(name, ())) if o not in merged)
        directives.append(f"{name} {' '.join(merged)}")
    return "; ".join(directives)


class ContentSecurityPolicyMiddleware:
    """Attach the strict CSP (or its report-only twin) to every response.

    ``CLINIC_CSP_REPORT_ONLY`` switches the header name only; the policy is
    identical. Settings refuse report-only outside synthetic data mode.
    """

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponseBase],
    ) -> None:
        """Store the downstream handler this boundary always wraps."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        """Set the CSP header on the inner response or refusal."""
        response = self.get_response(request)
        report_only: bool = settings.CLINIC_CSP_REPORT_ONLY
        header = CSP_REPORT_ONLY_HEADER if report_only else CSP_HEADER
        response.headers[header] = content_security_policy(request.path_info)
        return response


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
            publish_on_commit(topic="authz:halt", kind="halted", version=1)
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
