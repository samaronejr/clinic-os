from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Final, TypedDict

import sentry_sdk
from apps.core.telemetry import (
    SENTRY_ENVIRONMENTS,
    configure_tracing,
    scrub_sentry_event,
)

__all__ = [
    "REQUEST_CANARY_FIELDS",
    "configure_sentry",
    "configure_tracing",
    "scrub_sentry_event",
    "sentry_options",
]

if TYPE_CHECKING:
    from collections.abc import Callable

    from sentry_sdk.types import Event, Hint

_RELEASE_SHA: Final = re.compile(r"[0-9a-f]{40}")


class SentryOptions(TypedDict):
    """The exact production ``sentry_sdk.init`` keyword set."""

    dsn: str
    send_default_pii: bool
    include_local_variables: bool
    max_request_body_size: str
    before_send: Callable[[Event, Hint], Event | None]
    environment: str
    release: str | None


# Kept for callers/tests that reference the request-section canary set; the
# scrubber itself now applies the broader allowlist in apps.core.telemetry.
REQUEST_CANARY_FIELDS: Final = frozenset({"cookies", "data", "headers", "query_string"})


def sentry_options(dsn: str) -> SentryOptions:
    """Return the production Sentry client options (ADR-014).

    Environment and release are closed at configuration time: they also
    travel outside ``before_send`` (session attributes), so an operator
    value outside ``SENTRY_ENVIRONMENTS`` or a non-SHA release is replaced
    rather than exported.
    """
    environment = os.environ.get("SENTRY_ENVIRONMENT", "production")
    release = os.environ.get("SENTRY_RELEASE", "")
    return {
        "dsn": dsn,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "before_send": scrub_sentry_event,
        "environment": (
            environment if environment in SENTRY_ENVIRONMENTS else "production"
        ),
        "release": release if _RELEASE_SHA.fullmatch(release) else None,
    }


def configure_sentry(dsn: str) -> None:
    if dsn:
        sentry_sdk.init(**sentry_options(dsn))
