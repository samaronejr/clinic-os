"""Webhook egress boundary: the deterministic synthetic transport is the only one.

Real partner egress is BLOCKED-ON-EG: it needs an approved capability record,
an egress allowlist and a security review before any network call. Until then
live data mode refuses delivery and the synthetic transport records each signed
request in process so tests and rehearsals can verify it byte for byte.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode

if TYPE_CHECKING:
    from collections.abc import Mapping

type Outcome = Literal["delivered", "http_error", "timeout", "network_error"]
type Scripted = int | Literal["timeout", "network_error"]

HTTP_SUCCESS: Final = range(200, 300)


class WebhookTransportUnavailableError(Exception):
    """No approved transport exists for the current data mode."""


@dataclass(frozen=True, slots=True)
class TransportResult:
    """The classified outcome of one POST."""

    outcome: Outcome
    http_status: int | None


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """One request the synthetic transport received."""

    url: str
    headers: dict[str, str]
    body: bytes


@dataclass
class SyntheticWebhookTransport:
    """Record requests and answer from a scripted queue (default HTTP 200)."""

    requests: list[RecordedRequest] = field(default_factory=list)
    script: deque[Scripted] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> TransportResult:
        """Record the exact request bytes and classify the scripted answer."""
        del timeout
        with self._lock:
            self.requests.append(RecordedRequest(url, dict(headers), bytes(body)))
            answer: Scripted = self.script.popleft() if self.script else 200
        if answer == "timeout":
            return TransportResult("timeout", None)
        if answer == "network_error":
            return TransportResult("network_error", None)
        if answer in HTTP_SUCCESS:
            return TransportResult("delivered", answer)
        return TransportResult("http_error", answer)

    def reset(self) -> None:
        """Forget recorded requests and the scripted answers."""
        with self._lock:
            self.requests.clear()
            self.script.clear()


SYNTHETIC_TRANSPORT: Final = SyntheticWebhookTransport()


def transport() -> SyntheticWebhookTransport:
    """Return the synthetic transport; live egress has no approved transport."""
    if resolved_data_mode() != SYNTHETIC_DATA_MODE:
        raise WebhookTransportUnavailableError
    return SYNTHETIC_TRANSPORT
