"""Reference signer and verifier for ``Clinic-Signature: t=<ts>,v1=<hmac>``.

``v1`` is the lowercase hex HMAC-SHA256 of ``f"{t}."`` followed by the exact raw
request body, keyed by the subscription signing secret. During a secret rotation
the header carries one ``v1`` per valid secret. A receiver must verify the raw
bytes before parsing JSON, compare in constant time and refuse timestamps outside
its tolerance, so a captured delivery cannot be replayed later.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

HEADER: Final = "Clinic-Signature"
TOLERANCE_SECONDS: Final = 300
_TIMESTAMP: Final = re.compile(r"[0-9]{1,12}")
_HEX64: Final = re.compile(r"[0-9a-f]{64}")

type Rejection = Literal[
    "malformed", "timestamp_outside_tolerance", "signature_mismatch"
]
MALFORMED: Final[Rejection] = "malformed"
STALE: Final[Rejection] = "timestamp_outside_tolerance"
MISMATCH: Final[Rejection] = "signature_mismatch"


class WebhookSignatureError(Exception):
    """A refused delivery; ``reason`` is a closed machine code."""

    def __init__(self, reason: Rejection) -> None:
        """Keep only the closed reason; no header or body is reflected."""
        super().__init__(reason)
        self.reason: Rejection = reason


def _mac(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("ascii"), message, hashlib.sha256).hexdigest()


def signature_header(secrets: Sequence[str], timestamp: int, body: bytes) -> str:
    """Return the header value signing ``body`` with every valid secret."""
    signatures = ",".join(f"v1={_mac(secret, timestamp, body)}" for secret in secrets)
    return f"t={timestamp},{signatures}"


def verify(header: str, body: bytes, secret: str, *, now: int) -> None:
    """Raise ``WebhookSignatureError`` unless ``header`` signs ``body`` recently."""
    timestamp: int | None = None
    signatures: list[str] = []
    for item in header.split(","):
        key, separator, value = item.strip().partition("=")
        if not separator:
            raise WebhookSignatureError(MALFORMED)
        if key == "t" and timestamp is None and _TIMESTAMP.fullmatch(value):
            timestamp = int(value)
        elif key == "v1" and _HEX64.fullmatch(value):
            signatures.append(value)
        elif key in {"t", "v1"}:
            raise WebhookSignatureError(MALFORMED)
        # Unknown future schemes are ignored, never trusted.
    if timestamp is None or not signatures:
        raise WebhookSignatureError(MALFORMED)
    if abs(now - timestamp) > TOLERANCE_SECONDS:
        raise WebhookSignatureError(STALE)
    expected = _mac(secret, timestamp, body)
    if not any(hmac.compare_digest(expected, value) for value in signatures):
        raise WebhookSignatureError(MISMATCH)
