"""Deterministic signed OIDC fixture transport; real IdP legs are BLOCKED-ON-EG."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import ClassVar

from django.core import signing

from apps.identity.enterprise_authority import (
    GovernanceDeniedError,
    require_synthetic_identity,
)

CODE_SALT = "identity.enterprise.synthetic.oidc.v1"
MAX_CODE_BYTES = 16384
MAX_SUBJECT_LENGTH = 255
CEREMONY_TTL = 300


@dataclass(frozen=True, slots=True)
class FixtureIdentity:
    """Carry fixture IdP identity claims, not permission or membership authority."""

    subject: str
    groups: tuple[str, ...] = ()


def now_seconds() -> int:
    """Return UTC seconds through a replaceable clock, without test sleeps."""
    return int(time.time())


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256; the verifier remains exclusively in the server session."""
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


class SyntheticOidcAdapter:
    """Keep HTTP, real token endpoints and real credentials unreachable."""

    real_enabled: ClassVar = False

    @staticmethod
    def discovery(*, issuer: str) -> dict[str, object]:
        """Return the deterministic .invalid issuer's discovery fixture."""
        require_synthetic_identity()
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "jwks_uri": f"{issuer}/jwks",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        }

    @staticmethod
    def code_for(
        *,
        issuer: str,
        audience: str,
        identity: FixtureIdentity,
        nonce: str,
        challenge: str,
    ) -> str:
        """Issue fixture bytes only; this is not a real IdP or sandbox receipt."""
        require_synthetic_identity()
        return signing.dumps(
            {
                "iss": issuer,
                "aud": audience,
                "sub": identity.subject,
                "nonce": nonce,
                "challenge": challenge,
                "groups": list(identity.groups),
                "iat": now_seconds(),
                "exp": now_seconds() + CEREMONY_TTL,
            },
            salt=CODE_SALT,
        )

    @staticmethod
    def exchange(
        *, code: str, issuer: str, audience: str, nonce: str, verifier: str
    ) -> str:
        """Verify signed fixture bytes, exact issuer/audience/nonce, age and PKCE."""
        require_synthetic_identity()
        if not isinstance(code, str) or len(code) > MAX_CODE_BYTES:
            raise GovernanceDeniedError
        try:
            claims = signing.loads(code, salt=CODE_SALT, max_age=CEREMONY_TTL)
        except signing.BadSignature as error:
            raise GovernanceDeniedError from error
        if (
            not isinstance(claims, dict)
            or set(claims)
            != {"iss", "aud", "sub", "nonce", "challenge", "groups", "iat", "exp"}
            or claims["iss"] != issuer
            or claims["aud"] != audience
            or not isinstance(claims["sub"], str)
            or not 1 <= len(claims["sub"]) <= MAX_SUBJECT_LENGTH
            or not isinstance(claims["nonce"], str)
            or not hmac.compare_digest(claims["nonce"], nonce)
            or claims["challenge"] != pkce_challenge(verifier)
            or type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or not claims["iat"] <= now_seconds() < claims["exp"]
            or claims["exp"] - claims["iat"] > CEREMONY_TTL
        ):
            raise GovernanceDeniedError
        # Claims can identify a subject, never create or elevate a membership.
        return claims["sub"]
