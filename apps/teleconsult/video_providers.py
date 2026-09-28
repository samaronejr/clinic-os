"""The ``VideoProvider`` boundary: create a room, mint a short-lived token, revoke.

Only the synthetic provider is runnable. LiveKit and Twilio adapters build
the exact provider requests and tokens their public APIs define, but they
talk only through an injected ``Transport``; the default ``BlockedTransport``
never opens a socket (BLOCKED-ON-EG: EG-1 spend and EG-5 provider contract).
Tests drive them with recorded synthetic fixtures.

Room names are opaque (``tc-`` plus 32 hex), participant identities are the
role alone and tokens live at most 15 minutes, so no clinical identity
reaches a provider, a URL or a token. Recording is never requested: every
adapter asks the provider for a non-recording room.

Provider selection never falls back. When the registry reports the video
capability live, its current version must have a live adapter or the room is
refused; the synthetic provider serves only synthetic mode behind its gate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, NoReturn, Protocol
from urllib.parse import quote, urlencode

from django.core import signing

from apps.providers.models import CapabilityVersion
from apps.providers.services import current_version, is_live
from apps.teleconsult.capabilities import room_capability
from apps.teleconsult.migrations._provider_seed import SYNTHETIC_VIDEO_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

SYNTHETIC_PROVIDER: Final = SYNTHETIC_VIDEO_VERSION["provider"]
LIVEKIT_PROVIDER: Final = "LiveKit Cloud"
TWILIO_PROVIDER: Final = "Twilio Video"
TOKEN_TTL: Final = timedelta(minutes=15)
ADMIN_TOKEN_TTL: Final = timedelta(minutes=1)
ROOM_NAME: Final = re.compile(r"tc-[0-9a-f]{32}")
IDENTITIES: Final = frozenset({"physician", "patient"})
MAX_PARTICIPANTS: Final = 2
EMPTY_TIMEOUT_SECONDS: Final = 300
_SYNTHETIC_SALT: Final = "teleconsult-synthetic-video-token-v1"
_SYNTHETIC_STATES: Final = ("researched", "selected_in_plan")


class ProviderError(Exception):
    """A provider boundary failure; never carries provider payloads."""


class ProviderBlockedError(ProviderError):
    """Live provider traffic is blocked until the external gates clear."""

    def __init__(self) -> None:
        """Keep one fixed, payload-free message."""
        super().__init__("BLOCKED-ON-EG: live video provider unavailable")


class ProviderRequestError(ProviderError):
    """Invalid request input or an unexpected provider response."""


class TokenInvalidError(ProviderError):
    """A token is expired, tampered, or bound to another room."""


class ProviderUnavailableError(Exception):
    """No provider may create this room; never a silent fallback."""


@dataclass(frozen=True, slots=True)
class RoomSpec:
    """A non-recording two-participant room with an opaque name."""

    room_name: str

    def __post_init__(self) -> None:
        """Reject any name that is not the opaque room format."""
        if not isinstance(self.room_name, str) or not ROOM_NAME.fullmatch(
            self.room_name
        ):
            raise ProviderRequestError


@dataclass(frozen=True, slots=True)
class RevokeSpec:
    """Disconnect one role's participant from one room."""

    room_name: str
    identity: str

    def __post_init__(self) -> None:
        """Only opaque rooms and role identities cross the boundary."""
        RoomSpec(self.room_name)
        if self.identity not in IDENTITIES:
            raise ProviderRequestError


@dataclass(frozen=True, slots=True)
class TokenGrant:
    """A join grant: one room, one role identity, at most 15 minutes."""

    room_name: str
    identity: str
    issued_at: datetime
    ttl: timedelta = TOKEN_TTL

    def __post_init__(self) -> None:
        """Bound the lifetime and the identity before any signing."""
        RevokeSpec(self.room_name, self.identity)
        if (
            not isinstance(self.issued_at, datetime)
            or self.issued_at.tzinfo is None
            or not timedelta(0) < self.ttl <= TOKEN_TTL
        ):
            raise ProviderRequestError

    @property
    def expires_at(self) -> datetime:
        """The absolute expiry carried inside the token."""
        return self.issued_at + self.ttl


@dataclass(frozen=True, slots=True)
class RoomReceipt:
    """The provider's own reference; never a patient or session identifier."""

    provider: str
    reference: str


@dataclass(frozen=True, slots=True)
class MintedToken:
    """A signed join token and its expiry."""

    token: str
    expires_at: datetime


class VideoProvider(Protocol):
    """Provider-neutral room boundary; ``key`` equals the version's provider."""

    key: str

    def create_room(self, spec: RoomSpec) -> RoomReceipt:
        """Create one non-recording room; runs only outside transactions."""
        ...

    def mint_token(self, grant: TokenGrant) -> MintedToken:
        """Sign a short-lived join token locally; no network call."""
        ...

    def revoke(self, spec: RevokeSpec) -> RoomReceipt:
        """Disconnect one participant; runs only outside transactions."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """One outbound HTTP request, exactly as the provider receives it."""

    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """The provider's status and raw body."""

    status: int
    body: bytes


class Transport(Protocol):
    """Deliver one request; the only path from an adapter to a network."""

    def send(self, request: ProviderRequest) -> ProviderResponse:
        """Return the provider response or raise ``ProviderError``."""
        ...


class BlockedTransport:
    """The default transport: refuses every request without opening a socket."""

    def send(self, request: ProviderRequest) -> ProviderResponse:
        """Refuse; live provider legs are BLOCKED-ON-EG."""
        del request
        raise ProviderBlockedError


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def hs256_jwt(header: dict[str, str], claims: dict[str, object], secret: bytes) -> str:
    """Sign a compact HS256 JWT with canonical JSON segments."""
    signing_input = _b64(_json(header)) + "." + _b64(_json(claims))
    digest = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + _b64(digest)


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _response_json(response: ProviderResponse, expected: int) -> dict[str, object]:
    if response.status != expected:
        raise ProviderRequestError
    try:
        value = json.loads(response.body)
    except ValueError as error:
        raise ProviderRequestError from error
    if not isinstance(value, dict):
        raise ProviderRequestError
    return value


class SyntheticVideoProvider:
    """The only runnable provider: no network, signed synthetic tokens."""

    key: str = SYNTHETIC_PROVIDER

    def create_room(self, spec: RoomSpec) -> RoomReceipt:
        """Return an explicitly synthetic room reference."""
        return RoomReceipt(self.key, f"synthetic:room:{spec.room_name}")

    def mint_token(self, grant: TokenGrant) -> MintedToken:
        """Sign room, identity and expiry; nothing else is in the token."""
        token = signing.dumps(
            {
                "r": grant.room_name,
                "i": grant.identity,
                "e": _epoch(grant.expires_at),
            },
            salt=_SYNTHETIC_SALT,
            compress=False,
        )
        return MintedToken(token, grant.expires_at)

    def verify_token(self, token: str, *, room_name: str, now: datetime) -> str:
        """Return the identity of a live token for this room, else refuse."""
        try:
            value = signing.loads(token, salt=_SYNTHETIC_SALT)
        except signing.BadSignature as error:
            raise TokenInvalidError from error
        if (
            not isinstance(value, dict)
            or set(value) != {"r", "i", "e"}
            or value["r"] != room_name
            or value["i"] not in IDENTITIES
            or not isinstance(value["e"], int)
            or value["e"] <= _epoch(now)
        ):
            raise TokenInvalidError
        return str(value["i"])

    def revoke(self, spec: RevokeSpec) -> RoomReceipt:
        """Return an explicitly synthetic revoke reference."""
        return RoomReceipt(
            self.key, f"synthetic:revoke:{spec.room_name}:{spec.identity}"
        )


class LiveKitVideoProvider:
    """LiveKit Cloud RoomService (Twirp JSON) and access tokens.

    https://docs.livekit.io/reference/server/server-apis/ (CreateRoom,
    RemoveParticipant) and https://docs.livekit.io/home/get-started/authentication/
    (JWT: ``iss`` API key, ``sub`` identity, ``video`` grant). Recording on
    LiveKit is an explicit Egress request, which this adapter never makes.
    """

    key: str = LIVEKIT_PROVIDER

    def __init__(
        self,
        *,
        host: str,
        api_key: str,
        api_secret: bytes,
        transport: Transport,
        clock: Callable[[], datetime],
    ) -> None:
        """Bind an HTTPS host, synthetic-or-managed credentials and a transport."""
        if not host.startswith("https://") or not api_key or not api_secret:
            raise ProviderRequestError
        self._host = host.rstrip("/")
        self._api_key = api_key
        self._secret = api_secret
        self._transport = transport
        self._clock = clock

    def _admin(self, grant: dict[str, object]) -> str:
        now = self._clock()
        return hs256_jwt(
            {"alg": "HS256", "typ": "JWT"},
            {
                "iss": self._api_key,
                "nbf": _epoch(now),
                "exp": _epoch(now + ADMIN_TOKEN_TTL),
                "video": grant,
            },
            self._secret,
        )

    def _post(self, method: str, body: dict[str, object], admin: str) -> bytes:
        response = self._transport.send(
            ProviderRequest(
                "POST",
                f"{self._host}/twirp/livekit.RoomService/{method}",
                (
                    ("Authorization", "Bearer " + admin),
                    ("Content-Type", "application/json"),
                ),
                _json(body),
            )
        )
        return json.dumps(_response_json(response, 200)).encode("utf-8")

    def create_room(self, spec: RoomSpec) -> RoomReceipt:
        """CreateRoom with an empty-room timeout and two participants."""
        raw = self._post(
            "CreateRoom",
            {
                "name": spec.room_name,
                "empty_timeout": EMPTY_TIMEOUT_SECONDS,
                "max_participants": MAX_PARTICIPANTS,
            },
            self._admin({"roomCreate": True}),
        )
        sid = json.loads(raw).get("sid")
        if not isinstance(sid, str) or not re.fullmatch(r"RM_[A-Za-z0-9]{1,64}", sid):
            raise ProviderRequestError
        return RoomReceipt(self.key, f"livekit:room:{sid}")

    def mint_token(self, grant: TokenGrant) -> MintedToken:
        """Sign a join-only grant for one room; the identity is the role."""
        token = hs256_jwt(
            {"alg": "HS256", "typ": "JWT"},
            {
                "iss": self._api_key,
                "sub": grant.identity,
                "nbf": _epoch(grant.issued_at),
                "exp": _epoch(grant.expires_at),
                "video": {
                    "room": grant.room_name,
                    "roomJoin": True,
                    "canPublish": True,
                    "canSubscribe": True,
                    "canPublishData": False,
                    "roomRecord": False,
                },
            },
            self._secret,
        )
        return MintedToken(token, grant.expires_at)

    def revoke(self, spec: RevokeSpec) -> RoomReceipt:
        """RemoveParticipant for one role identity."""
        self._post(
            "RemoveParticipant",
            {"room": spec.room_name, "identity": spec.identity},
            self._admin({"roomAdmin": True, "room": spec.room_name}),
        )
        return RoomReceipt(self.key, f"livekit:revoke:{spec.room_name}:{spec.identity}")


class TwilioVideoProvider:
    """Twilio Programmable Video REST v1 and access tokens.

    https://www.twilio.com/docs/video/api/rooms-resource (create, with
    ``RecordParticipantsOnConnect=false``),
    https://www.twilio.com/docs/video/api/participants (``Status=disconnected``)
    and https://www.twilio.com/docs/iam/access-tokens (``cty twilio-fpa;v=1``).
    """

    key: str = TWILIO_PROVIDER
    _API: Final = "https://video.twilio.com/v1"

    def __init__(
        self,
        *,
        account_sid: str,
        api_key_sid: str,
        api_key_secret: str,
        transport: Transport,
        clock: Callable[[], datetime],
    ) -> None:
        """Bind account, API key credentials and a transport."""
        if not account_sid or not api_key_sid or not api_key_secret:
            raise ProviderRequestError
        self._account = account_sid
        self._key = api_key_sid
        self._secret = api_key_secret
        self._transport = transport
        self._clock = clock

    def _basic(self) -> str:
        pair = f"{self._key}:{self._secret}".encode()
        return "Basic " + base64.b64encode(pair).decode("ascii")

    def _post(self, url: str, form: dict[str, str], expected: int) -> dict[str, object]:
        response = self._transport.send(
            ProviderRequest(
                "POST",
                url,
                (
                    ("Authorization", self._basic()),
                    ("Content-Type", "application/x-www-form-urlencoded"),
                ),
                urlencode(sorted(form.items())).encode("ascii"),
            )
        )
        return _response_json(response, expected)

    def create_room(self, spec: RoomSpec) -> RoomReceipt:
        """Create a group room that never records participants."""
        value = self._post(
            f"{self._API}/Rooms",
            {
                "UniqueName": spec.room_name,
                "Type": "group",
                "MaxParticipants": str(MAX_PARTICIPANTS),
                "RecordParticipantsOnConnect": "false",
                "EmptyRoomTimeout": str(EMPTY_TIMEOUT_SECONDS // 60),
            },
            201,
        )
        sid = value.get("sid")
        if not isinstance(sid, str) or not re.fullmatch(r"RM[0-9a-f]{32}", sid):
            raise ProviderRequestError
        return RoomReceipt(self.key, f"twilio:room:{sid}")

    def mint_token(self, grant: TokenGrant) -> MintedToken:
        """Sign an access token with a single video grant for one room."""
        nbf = _epoch(grant.issued_at)
        token = hs256_jwt(
            {"alg": "HS256", "typ": "JWT", "cty": "twilio-fpa;v=1"},
            {
                "jti": f"{self._key}-{nbf}",
                "iss": self._key,
                "sub": self._account,
                "nbf": nbf,
                "exp": _epoch(grant.expires_at),
                "grants": {
                    "identity": grant.identity,
                    "video": {"room": grant.room_name},
                },
            },
            self._secret.encode("utf-8"),
        )
        return MintedToken(token, grant.expires_at)

    def revoke(self, spec: RevokeSpec) -> RoomReceipt:
        """Disconnect one role identity from the named room."""
        self._post(
            f"{self._API}/Rooms/{quote(spec.room_name, safe='')}/Participants/"
            f"{quote(spec.identity, safe='')}",
            {"Status": "disconnected"},
            200,
        )
        return RoomReceipt(self.key, f"twilio:revoke:{spec.room_name}:{spec.identity}")


LIVE_PROVIDERS: Final = frozenset({LIVEKIT_PROVIDER, TWILIO_PROVIDER})


def live_provider(provider: str) -> NoReturn:
    """Construct a live adapter; BLOCKED-ON-EG, so this always refuses.

    No managed secret backend or approved provider contract exists, so there
    are no credentials to bind and no transport may reach a provider. An
    unknown provider is refused the same way; nothing falls back.
    """
    del provider
    raise ProviderBlockedError


@dataclass(frozen=True, slots=True)
class ProviderChoice:
    """The ``video`` capability version a new room binds to, by natural identity.

    Recovery re-seeds the registry under fresh surrogate keys, so rooms store
    the version's provider and environment rather than its UUID.
    """

    provider: str
    environment: str


def select_room_provider(*, clinic_id: UUID) -> ProviderChoice:
    """Pick the room provider from the registry; refuse rather than fall back.

    A live video capability must be served by its own current version's
    adapter (today always blocked). Outside live mode only the explicitly
    enabled synthetic provider can serve, through its dedicated version.
    """
    if is_live("video", clinic_id=clinic_id):
        version = current_version("video", clinic_id=clinic_id)
        if (
            version is None
            or version.provider not in LIVE_PROVIDERS
            or version.environment == SYNTHETIC_VIDEO_VERSION["environment"]
        ):
            raise ProviderUnavailableError
        try:
            live_provider(version.provider)
        except ProviderBlockedError as error:
            raise ProviderUnavailableError from error
    if not room_capability().synthetic_enabled:
        raise ProviderUnavailableError
    version = CapabilityVersion.objects.filter(
        capability__key="video",
        capability__clinic_id__isnull=True,
        state__in=_SYNTHETIC_STATES,
        **SYNTHETIC_VIDEO_VERSION,
    ).first()
    if version is None:
        raise ProviderUnavailableError
    return ProviderChoice(version.provider, version.environment)


def provider_for_room(provider: str, environment: str) -> VideoProvider:
    """Return the adapter that owns rooms bound to this version identity.

    Legacy rooms stored before versions were bound (empty identity) were
    synthetic by construction: the v1 trigger admitted no other provider.
    """
    if (provider, environment) in {
        ("", ""),
        (SYNTHETIC_PROVIDER, SYNTHETIC_VIDEO_VERSION["environment"]),
    }:
        return SyntheticVideoProvider()
    return live_provider(provider)


def utc_now() -> datetime:
    """Return the adapter clock reading (a seam for recorded fixtures)."""
    return datetime.now(UTC)
