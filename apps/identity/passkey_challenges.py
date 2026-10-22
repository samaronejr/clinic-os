"""Server-session WebAuthn challenges with atomic, bounded single use."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Never
from urllib.parse import urlsplit
from uuid import UUID

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models.functions import Now
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

if TYPE_CHECKING:
    from django.http import HttpRequest

CHALLENGE_KEY: Final = "passkey_challenge"
PASSKEY_SESSION_KEY: Final = "passkey_verified_id"
STEP_UP_SESSION_KEY: Final = "otp_verified_at"
CHALLENGE_TTL: Final = 300
MAX_CREDENTIAL_JSON: Final = 32768
MAX_CREDENTIAL_ID: Final = 1023
MAX_NICKNAME: Final = 80
MAX_TRANSPORTS: Final = 8
type Ceremony = Literal["register", "login", "step_up"]


class PasskeyDeniedError(PermissionDenied):
    """Refuse invalid, foreign, expired or replayed ceremonies identically."""


def deny_passkey() -> Never:
    """Use one fail-closed refusal inside ceremony handlers."""
    raise PasskeyDeniedError


@dataclass(frozen=True, slots=True)
class Challenge:
    """Trusted ceremony binding recovered only from a locked server session."""

    challenge: bytes = field(repr=False)
    purpose: Ceremony
    rp_id: str
    origin: str
    issued_at: int
    user_id: UUID | None = None
    organization_id: UUID | None = None
    clinic_id: UUID | None = None
    target: str = "/workspace/"


def now_seconds() -> int:
    """Keep one replaceable clock seam for deterministic expiry tests."""
    return int(time.time())


def new_challenge(request: HttpRequest, *, purpose: Ceremony) -> Challenge:
    """Bind the configured RP and exact origin, never a posted origin claim."""
    rp_id = settings.WEBAUTHN_RP_ID
    origin = settings.WEBAUTHN_ORIGIN
    if not isinstance(rp_id, str) or not isinstance(origin, str):
        raise PasskeyDeniedError
    if not origin:
        if (
            settings.CLINIC_DATA_MODE != "synthetic"
            or rp_id not in {"localhost", "127.0.0.1", "::1"}
            or request.get_host().split(":", 1)[0] != rp_id
        ):
            raise PasskeyDeniedError
        origin = request.build_absolute_uri("/").rstrip("/")
    parsed = urlsplit(origin)
    if (
        not rp_id
        or parsed.hostname is None
        or not (parsed.hostname == rp_id or parsed.hostname.endswith("." + rp_id))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.scheme not in {"http", "https"}
        or (settings.CLINIC_DATA_MODE == "live" and parsed.scheme != "https")
        or settings.SESSION_ENGINE != "django.contrib.sessions.backends.db"
    ):
        raise PasskeyDeniedError
    return Challenge(
        challenge=secrets.token_bytes(32),
        purpose=purpose,
        rp_id=rp_id,
        origin=origin,
        issued_at=now_seconds(),
    )


def store_challenge(request: HttpRequest, *, challenge: Challenge) -> None:
    """Persist only on the server; the browser receives options, not authority."""
    request.session[CHALLENGE_KEY] = {
        "challenge": bytes_to_base64url(challenge.challenge),
        "purpose": challenge.purpose,
        "rp_id": challenge.rp_id,
        "origin": challenge.origin,
        "issued_at": challenge.issued_at,
        "user_id": str(challenge.user_id) if challenge.user_id else None,
        "organization_id": (
            str(challenge.organization_id) if challenge.organization_id else None
        ),
        "clinic_id": str(challenge.clinic_id) if challenge.clinic_id else None,
        "target": challenge.target,
    }
    request.session.save()


def consume_challenge(request: HttpRequest, *, purpose: Ceremony) -> Challenge:
    """Consume the persisted nonce under a row lock, including zero counters."""
    key = request.session.session_key
    if key is None:
        raise PasskeyDeniedError
    with transaction.atomic():
        persisted = (
            Session.objects.select_for_update()
            .filter(session_key=key, expire_date__gt=Now())
            .first()
        )
        if persisted is None:
            raise PasskeyDeniedError
        data = persisted.get_decoded()
        raw = data.pop(CHALLENGE_KEY, None)
        if raw is None:
            raise PasskeyDeniedError
        fresh = SessionStore(session_key=key)
        fresh.update(data)
        fresh.pop(CHALLENGE_KEY, None)
        fresh.save()
        request.session.pop(CHALLENGE_KEY, None)
    if (
        not isinstance(raw, dict)
        or raw.get("purpose") != purpose
        or not isinstance(raw.get("challenge"), str)
        or not isinstance(raw.get("rp_id"), str)
        or not isinstance(raw.get("origin"), str)
        or type(raw.get("issued_at")) is not int
        or not isinstance(raw.get("target"), str)
    ):
        raise PasskeyDeniedError
    age = now_seconds() - raw["issued_at"]
    if age < 0 or age > CHALLENGE_TTL:
        raise PasskeyDeniedError
    try:
        return Challenge(
            challenge=base64url_to_bytes(raw["challenge"]),
            purpose=purpose,
            rp_id=raw["rp_id"],
            origin=raw["origin"],
            issued_at=raw["issued_at"],
            user_id=UUID(raw["user_id"]) if raw.get("user_id") else None,
            organization_id=(
                UUID(raw["organization_id"]) if raw.get("organization_id") else None
            ),
            clinic_id=UUID(raw["clinic_id"]) if raw.get("clinic_id") else None,
            target=raw["target"],
        )
    except (ValueError, TypeError) as error:
        raise PasskeyDeniedError from error
