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
    "drop_sentry_transaction",
    "scrub_sentry_event",
    "sentry_options",
]

if TYPE_CHECKING:
    from collections.abc import Callable

    from sentry_sdk.types import Event, Hint

_RELEASE_SHA: Final = re.compile(r"[0-9a-f]{40}")
# Fixed values used instead of ``None``: for ``release`` and ``server_name``
# the SDK treats ``None`` as "infer it" and reads SENTRY_RELEASE, git, CI
# variables or the hostname, which bypasses validation and reaches session
# envelopes outside ``before_send``.
UNVERSIONED_RELEASE: Final = "unversioned"
SERVER_NAME: Final = "clinic-os"


class SentryOptions(TypedDict):
    """The exact production ``sentry_sdk.init`` keyword set."""

    dsn: str
    send_default_pii: bool
    include_local_variables: bool
    max_request_body_size: str
    before_send: Callable[[Event, Hint], Event | None]
    before_send_transaction: Callable[[Event, Hint], Event | None]
    environment: str
    release: str
    server_name: str
    auto_session_tracking: bool
    spotlight: bool


def drop_sentry_transaction(_event: Event, _hint: Hint) -> Event | None:
    """Drop every transaction: spans carry URLs, SQL and task arguments."""
    return None


# Kept for callers/tests that reference the request-section canary set; the
# scrubber itself now applies the broader allowlist in apps.core.telemetry.
REQUEST_CANARY_FIELDS: Final = frozenset({"cookies", "data", "headers", "query_string"})


def sentry_options(dsn: str) -> SentryOptions:
    """Return the production Sentry client options (ADR-014).

    Every option the SDK would otherwise infer from the environment is passed
    explicitly, because inferred values reach envelopes that never pass
    through ``before_send`` (sessions, transactions, client reports):

    - ``environment``: ``SENTRY_ENVIRONMENT`` when in ``SENTRY_ENVIRONMENTS``,
      else ``production``;
    - ``release``: ``SENTRY_RELEASE`` when it is a 40-hex SHA, else the fixed
      ``UNVERSIONED_RELEASE`` (never ``None``, which re-reads the raw value);
    - ``server_name``: fixed, never the hostname;
    - automatic session tracking and Spotlight mirroring are off, and
      transactions are dropped.
    """
    environment = os.environ.get("SENTRY_ENVIRONMENT", "production")
    release = os.environ.get("SENTRY_RELEASE", "")
    return {
        "dsn": dsn,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "before_send": scrub_sentry_event,
        "before_send_transaction": drop_sentry_transaction,
        "environment": (
            environment if environment in SENTRY_ENVIRONMENTS else "production"
        ),
        "release": release if _RELEASE_SHA.fullmatch(release) else UNVERSIONED_RELEASE,
        "server_name": SERVER_NAME,
        "auto_session_tracking": False,
        "spotlight": False,
    }


def configure_sentry(dsn: str) -> None:
    if dsn:
        sentry_sdk.init(**sentry_options(dsn))
