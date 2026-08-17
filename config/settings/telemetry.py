from __future__ import annotations

from typing import TYPE_CHECKING, Final

import sentry_sdk

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint

REQUEST_CANARY_FIELDS: Final = frozenset({"cookies", "data", "headers", "query_string"})


def scrub_sentry_event(
    event: Event,
    _hint: Hint,
) -> Event:
    scrubbed = event.copy()
    request = event.get("request")
    if isinstance(request, dict):
        clean_request = {
            key: value
            for key, value in request.items()
            if isinstance(key, str) and key not in REQUEST_CANARY_FIELDS
        }
        scrubbed["request"] = clean_request
    return scrubbed


def configure_sentry(dsn: str) -> None:
    if dsn:
        sentry_sdk.init(
            dsn=dsn,
            send_default_pii=False,
            include_local_variables=False,
            max_request_body_size="never",
            before_send=scrub_sentry_event,
        )
