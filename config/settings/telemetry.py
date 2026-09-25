from __future__ import annotations

from typing import Final

import sentry_sdk
from apps.core.telemetry import configure_tracing, scrub_sentry_event

__all__ = [
    "REQUEST_CANARY_FIELDS",
    "configure_sentry",
    "configure_tracing",
    "scrub_sentry_event",
]

# Kept for callers/tests that reference the request-section canary set; the
# scrubber itself now applies the broader allowlist in apps.core.telemetry.
REQUEST_CANARY_FIELDS: Final = frozenset({"cookies", "data", "headers", "query_string"})


def configure_sentry(dsn: str) -> None:
    if dsn:
        sentry_sdk.init(
            dsn=dsn,
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            before_send=scrub_sentry_event,
        )
