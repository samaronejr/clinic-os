"""Authentication redirect validation kept separate from OTP state changes."""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING, Final
from urllib.parse import unquote, urlsplit

from django.utils.http import url_has_allowed_host_and_scheme

if TYPE_CHECKING:
    from django.http import HttpRequest

AUTH_FLOW_PATHS: Final = frozenset(
    {
        "/auth/login",
        "/auth/enroll",
        "/auth/verify",
        "/auth/logout",
        "/auth/step-up",
    }
)
DEFAULT_AUTH_TARGET: Final = "/auth/protected/"
ASCII_CONTROL_LIMIT: Final = 0x20
ASCII_DELETE: Final = 0x7F


def _canonical_target(raw_target: str) -> tuple[str, str] | None:
    decoded_target = raw_target
    for _attempt in range(4):
        try:
            next_target = unquote(decoded_target, errors="strict")
        except UnicodeDecodeError:
            return None
        if next_target == decoded_target:
            break
        decoded_target = next_target
    else:
        return None
    if "\\" in decoded_target or any(
        ord(character) < ASCII_CONTROL_LIMIT or ord(character) == ASCII_DELETE
        for character in decoded_target
    ):
        return None
    path = urlsplit(decoded_target).path
    if not path.startswith("/"):
        return None
    canonical_path = posixpath.normpath(f"/{path.lstrip('/')}")
    return decoded_target, canonical_path.rstrip("/") or "/"


def safe_next_url(request: HttpRequest, raw_target: str | None) -> str:
    """Accept only a same-host non-auth-flow redirect target."""
    if not raw_target:
        return DEFAULT_AUTH_TARGET
    canonical_target = _canonical_target(raw_target)
    if canonical_target is None:
        return DEFAULT_AUTH_TARGET
    decoded_target, canonical_path = canonical_target
    allowed_hosts = {request.get_host()}
    require_https = request.is_secure()
    if not all(
        url_has_allowed_host_and_scheme(
            target,
            allowed_hosts=allowed_hosts,
            require_https=require_https,
        )
        for target in (raw_target, decoded_target)
    ):
        return DEFAULT_AUTH_TARGET
    if canonical_path in AUTH_FLOW_PATHS:
        return DEFAULT_AUTH_TARGET
    return raw_target
