"""Proof shared only by a synthetic browser launcher and its Gunicorn child."""

import hashlib
import hmac
import re
import secrets
from typing import Final

READINESS_KEY: Final = "CLINIC_BROWSER_READINESS_KEY"
CHALLENGE_HEADER: Final = "X-Clinic-Browser-Challenge"
PROOF_HEADER: Final = "X-Clinic-Browser-Proof"
WSGI_APPLICATION: Final = "ops.testing.browser_readiness_wsgi:application"


def new_readiness_key() -> str:
    """Return independent entropy for each supervised launch."""
    return secrets.token_hex(32)


def readiness_proof(key: str, challenge: str) -> str:
    """Authenticate a single probe without sending the per-launch key."""
    return hmac.new(
        key.encode("ascii"), challenge.encode("ascii"), hashlib.sha256
    ).hexdigest()


def valid_challenge(value: object) -> bool:
    """Only accept the fixed-size ASCII challenge emitted by the launcher."""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
