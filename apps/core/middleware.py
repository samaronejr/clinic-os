"""Outermost response privacy boundary for every product path."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from django.utils.cache import patch_cache_control, patch_vary_headers

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponseBase

PUBLIC_PREFIXES: Final = ("/static/",)
PRIVATE_VARY_HEADERS: Final = ("Cookie", "HX-Request")


def is_private_product_path(path: str) -> bool:
    """Report whether one request path carries private clinical product state."""
    return not path.startswith(PUBLIC_PREFIXES)


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
