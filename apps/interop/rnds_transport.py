"""RNDS exchange transports: recorded fixtures (the reachable leg) and mTLS.

Per the RNDS guide, ``GET /api/token`` on the Auth host needs the
establishment's ICP-Brasil certificate (mutual TLS) and returns a token valid
for 30 minutes; ``POST /api/fhir/r4/Bundle`` on the EHR host then carries
``X-Authorization-Server: Bearer <token>`` and ``Authorization: <requester
CNS>``. An accepted bundle's RNDS identifier follows the last ``/`` of the
``location`` header; a refusal is a FHIR ``OperationOutcome`` whose diagnostics
start with an ``(EHR-ERRnnn)`` code. Only that code is kept, never the text.

``MtlsRndsTransport`` implements this with the standard library (``ssl``,
``http.client``); ``cryptography``, already a locked dependency, only opens the
PKCS#12 bundle, because ``ssl`` cannot. Nothing selects it: with EG-7 unmet,
``transport_for`` returns the fixture transport for the fixture provider and
refuses every other provider.
"""

from __future__ import annotations

import http.client
import json
import re
import secrets
import ssl
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

from config.settings.contracts import SYNTHETIC_DATA_MODE, resolved_data_mode

from apps.core.secrets import SecretUnavailableError, credential_store
from apps.interop.errors import RndsUnavailableError
from apps.interop.rnds_bundle import (
    REQUESTER_SYSTEM,
    Establishment,
    cns_valid,
    validate_bundle,
)
from apps.interop.rnds_gate import FIXTURE_PROVIDER

if TYPE_CHECKING:
    from uuid import UUID

    from apps.interop.adapters import InteropAdapter

type TransportFailure = Literal["network", "timeout", "server", "credential"]
NETWORK: Final[TransportFailure] = "network"
TIMEOUT: Final[TransportFailure] = "timeout"
SERVER: Final[TransportFailure] = "server"
CREDENTIAL: Final[TransportFailure] = "credential"
# ``outage``, ``timeout``, ``network``, ``token_expired`` or a recorded EHR code.
type Scripted = str
_SCRIPTED_FAILURES: Final[dict[str, TransportFailure]] = {
    "outage": SERVER,
    "timeout": TIMEOUT,
    "network": NETWORK,
}

TOKEN_PATH: Final = "/api/token"  # noqa: S105 - a path, not a credential
BUNDLE_PATH: Final = "/api/fhir/r4/Bundle"
TOKEN_REFRESH_MARGIN_SECONDS: Final = 60.0
TOKEN_MAX_SECONDS: Final = 1800.0
MAX_ANSWER_BYTES: Final = 1_048_576
_ANSWER_CODE: Final = re.compile(r"\((EHR-ERR[0-9]{3})\)")
_RNDS_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9.-]{1,128}")
_TOKEN: Final = re.compile(r"[\x21-\x7e]{1,8192}")
_FIXTURES: Final = Path(__file__).with_name("rnds_fixtures")
_FIXTURE_NAMESPACE: Final = uuid.UUID("7d6b3c2e-6f0a-4b51-9a3e-5d2f1c0b8a47")
HTTP_OK: Final = 200
HTTP_CREATED: Final = 201
HTTP_UNAUTHORIZED: Final = 401
HTTP_SERVER_ERROR: Final = 500
HTTP_UNPROCESSABLE: Final = 422
TOKEN_EXPIRED: Final = "EHR-ERR882"  # noqa: S105 - an RNDS answer code
# Synthetic establishment of the fixture leg; never a real requester or CNES.
FIXTURE_ESTABLISHMENT: Final = Establishment(
    requester_id="00000",
    cnes="0000000",
    performer="11222333000181",
    requester_cns="700000000000005",
)


class RndsTransportError(Exception):
    """A transport failure without an RNDS answer, classified for the outbox."""

    def __init__(self, kind: TransportFailure) -> None:
        """Bind one closed failure kind; never a host, header or body."""
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class RndsAnswer:
    """The classified RNDS answer: HTTP status, identifier and EHR codes only."""

    status: int
    identifier: str = ""
    codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RndsEndpoints:
    """The Auth and EHR hosts of one RNDS environment."""

    auth_host: str
    ehr_host: str
    auth_port: int = 443
    ehr_port: int = 443


# The guide's homologation hosts; production is per state and BLOCKED-ON-EG-7.
HOMOLOGATION: Final = RndsEndpoints(
    auth_host="ehr-auth-hmg.saude.gov.br", ehr_host="ehr-services.hmg.saude.gov.br"
)


@dataclass(frozen=True, slots=True)
class RndsCredential:
    """The establishment's PKCS#12 bundle and passphrase from the secret store."""

    pkcs12: bytes = field(repr=False)
    passphrase: bytes = field(repr=False)


def credential_names(clinic_id: UUID) -> tuple[str, str]:
    """Return the secret-store credential names of one clinic's RNDS certificate."""
    return (f"rnds-{clinic_id.hex}-pkcs12", f"rnds-{clinic_id.hex}-passphrase")


def load_credential(clinic_id: UUID) -> RndsCredential:
    """Read the clinic's certificate from the secret store; never the database."""
    bundle_name, passphrase_name = credential_names(clinic_id)
    try:
        store = credential_store()
        pkcs12 = store.get_credential(bundle_name)
        passphrase = store.get_credential(passphrase_name).rstrip(b"\r\n")
    except SecretUnavailableError as error:
        raise RndsTransportError(CREDENTIAL) from error
    if not passphrase:
        raise RndsTransportError(CREDENTIAL)
    return RndsCredential(pkcs12=pkcs12, passphrase=passphrase)


def parse_answer(status: int, location: str, body: bytes) -> RndsAnswer:
    """Keep the status, the identifier after the last ``/`` and the EHR codes."""
    identifier = ""
    if status == HTTP_CREATED:
        candidate = location.rsplit("/", 1)[-1]
        identifier = candidate if _RNDS_IDENTIFIER.fullmatch(candidate) else ""
    codes: list[str] = []
    try:
        outcome = json.loads(body) if body else None
    except (UnicodeDecodeError, ValueError):
        outcome = None
    if isinstance(outcome, dict) and outcome.get("resourceType") == "OperationOutcome":
        for issue in outcome.get("issue") or ():
            diagnostics = issue.get("diagnostics") if isinstance(issue, dict) else None
            if isinstance(diagnostics, str) and (
                found := _ANSWER_CODE.search(diagnostics)
            ):
                codes.append(found.group(1))
    return RndsAnswer(status=status, identifier=identifier, codes=tuple(codes))


def _client_context(
    credential: RndsCredential, *, cafile: str | None
) -> ssl.SSLContext:
    """Build the mutual-TLS context; the key touches disk only re-encrypted."""
    # Imported here so the fixture leg never loads certificate machinery.
    from cryptography.hazmat.primitives.serialization import (  # noqa: PLC0415
        BestAvailableEncryption,
        Encoding,
        PrivateFormat,
        pkcs12,
    )

    try:
        key, certificate, chain = pkcs12.load_key_and_certificates(
            credential.pkcs12, credential.passphrase
        )
    except (TypeError, ValueError) as error:
        raise RndsTransportError(CREDENTIAL) from error
    if key is None or certificate is None:
        raise RndsTransportError(CREDENTIAL)
    now = datetime.now(UTC)
    if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
        raise RndsTransportError(CREDENTIAL)
    ephemeral = secrets.token_hex(32).encode("ascii")
    pem_key = key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, BestAvailableEncryption(ephemeral)
    )
    pem_chain = certificate.public_bytes(Encoding.PEM) + b"".join(
        extra.public_bytes(Encoding.PEM) for extra in chain or ()
    )
    context = ssl.create_default_context(cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory(prefix="rnds-credential-") as work:
        key_path = Path(work) / "key.pem"
        chain_path = Path(work) / "chain.pem"
        for path, content in ((key_path, pem_key), (chain_path, pem_chain)):
            path.touch(mode=0o600, exist_ok=False)
            path.write_bytes(content)
        try:
            context.load_cert_chain(chain_path, key_path, password=ephemeral)
        except ssl.SSLError as error:
            raise RndsTransportError(CREDENTIAL) from error
    return context


class MtlsRndsTransport:
    """Token over mutual TLS, then one bundle POST; standard library only."""

    def __init__(
        self,
        *,
        endpoints: RndsEndpoints,
        credential: RndsCredential,
        cafile: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        """Bind one environment and credential; nothing connects until a send."""
        self._endpoints = endpoints
        self._credential = credential
        self._cafile = cafile
        self._timeout = timeout
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _access_token(self) -> str:
        with self._lock:
            if self._token is not None and time.monotonic() < self._token[1]:
                return self._token[0]
        context = _client_context(self._credential, cafile=self._cafile)
        connection = http.client.HTTPSConnection(
            self._endpoints.auth_host,
            self._endpoints.auth_port,
            context=context,
            timeout=self._timeout,
        )
        try:
            connection.request(
                "GET", TOKEN_PATH, headers={"Accept": "application/json"}
            )
            response = connection.getresponse()
            body = response.read(MAX_ANSWER_BYTES)
        except ssl.SSLError as error:
            raise RndsTransportError(CREDENTIAL) from error
        except OSError as error:
            raise RndsTransportError(NETWORK) from error
        finally:
            connection.close()
        if response.status >= HTTP_SERVER_ERROR:
            raise RndsTransportError(SERVER)
        try:
            payload = json.loads(body) if response.status == HTTP_OK else None
        except (UnicodeDecodeError, ValueError):
            payload = None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        lifetime = payload.get("expires_in") if isinstance(payload, dict) else None
        if (
            not isinstance(token, str)
            or not _TOKEN.fullmatch(token)
            or type(lifetime) is not int
            or lifetime <= 0
        ):
            raise RndsTransportError(CREDENTIAL)
        seconds = (
            min(lifetime / 1000.0, TOKEN_MAX_SECONDS) - TOKEN_REFRESH_MARGIN_SECONDS
        )
        with self._lock:
            self._token = (token, time.monotonic() + max(seconds, 0.0))
        return token

    def _post(self, body: bytes, requester_cns: str, token: str) -> RndsAnswer:
        context = ssl.create_default_context(cafile=self._cafile)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = http.client.HTTPSConnection(
            self._endpoints.ehr_host,
            self._endpoints.ehr_port,
            context=context,
            timeout=self._timeout,
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Authorization-Server": f"Bearer {token}",
            "Authorization": requester_cns,
        }
        try:
            try:
                connection.request("POST", BUNDLE_PATH, body=body, headers=headers)
            except ssl.SSLError as error:
                raise RndsTransportError(CREDENTIAL) from error
            except OSError as error:
                raise RndsTransportError(NETWORK) from error
            try:
                response = connection.getresponse()
                answer = response.read(MAX_ANSWER_BYTES)
            except OSError as error:
                # The bundle may have arrived; its identifier makes a retry safe.
                raise RndsTransportError(TIMEOUT) from error
        finally:
            connection.close()
        if response.status >= HTTP_SERVER_ERROR:
            raise RndsTransportError(SERVER)
        return parse_answer(response.status, response.getheader("location", ""), answer)

    def submit_bundle(self, *, body: bytes, requester_cns: str) -> RndsAnswer:
        """Send one bundle, refreshing an expired token exactly once."""
        answer = self._post(body, requester_cns, self._access_token())
        if answer.status == HTTP_UNAUTHORIZED and TOKEN_EXPIRED in answer.codes:
            with self._lock:
                self._token = None
            answer = self._post(body, requester_cns, self._access_token())
        return answer


@cache
def recorded_answers() -> dict[str, dict[str, object]]:
    """Load the RNDS answers recorded from the guide's documentation examples."""
    return cast(
        "dict[str, dict[str, object]]",
        json.loads((_FIXTURES / "responses.json").read_text(encoding="utf-8")),
    )


@dataclass(frozen=True, slots=True)
class RecordedRndsRequest:
    """One request the fixture transport received."""

    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class FixtureRndsTransport:
    """Answer like RNDS homologation from the recorded documentation examples.

    A valid bundle from the fixture establishment is accepted once per bundle
    identifier; a repeat gets the recorded duplicate answer (EHR-ERR866), a
    non-final document EHR-ERR924, a foreign requester system EHR-ERR881 and an
    unknown requester CNS EHR-ERR906. Tests may script an outage, a timeout, a
    network failure, an expired token or a recorded outcome code instead.
    """

    requests: list[RecordedRndsRequest] = field(default_factory=list)
    script: deque[Scripted] = field(default_factory=deque)
    accepted: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _outcome(self, code: str) -> RndsAnswer:
        recorded = cast("dict[str, object]", recorded_answers()["outcomes"][code])
        status = cast("int", recorded["status"])
        return parse_answer(status, "", json.dumps(recorded["body"]).encode())

    @staticmethod
    def _refusal(bundle: object, requester_cns: str) -> str | None:
        """Return the recorded refusal this bundle earns, or None to accept it."""
        issues = validate_bundle(bundle)
        identifier = bundle.get("identifier") if isinstance(bundle, dict) else None
        system = identifier.get("system") if isinstance(identifier, dict) else None
        refusal = None
        if any(issue.code == "status_not_final" for issue in issues):
            refusal = "EHR-ERR924"
        elif system != REQUESTER_SYSTEM + FIXTURE_ESTABLISHMENT.requester_id:
            refusal = "EHR-ERR881"
        elif requester_cns != FIXTURE_ESTABLISHMENT.requester_cns or not cns_valid(
            requester_cns
        ):
            refusal = "EHR-ERR906"
        elif issues:
            refusal = ""
        return refusal

    def _judge(self, body: bytes, requester_cns: str) -> RndsAnswer:
        try:
            bundle = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            return RndsAnswer(status=HTTP_UNPROCESSABLE)
        refusal = self._refusal(bundle, requester_cns)
        if refusal is not None:
            return self._outcome(refusal) if refusal else RndsAnswer(HTTP_UNPROCESSABLE)
        value = str(bundle["identifier"]["value"])
        if value in self.accepted:
            return self._outcome("EHR-ERR866")
        assigned = f"{uuid.uuid5(_FIXTURE_NAMESPACE, value)}-{value[-4:]}"
        self.accepted[value] = assigned
        prefix = str(recorded_answers()["accepted"]["location_prefix"])
        return parse_answer(HTTP_CREATED, prefix + assigned, b"")

    def submit_bundle(self, *, body: bytes, requester_cns: str) -> RndsAnswer:
        """Record the exact request bytes, then answer as recorded or scripted."""
        headers = {
            "X-Authorization-Server": "Bearer fixture-access-token",
            "Authorization": requester_cns,
        }
        with self._lock:
            self.requests.append(RecordedRndsRequest(BUNDLE_PATH, headers, bytes(body)))
            scripted = self.script.popleft() if self.script else None
            if scripted == "token_expired":
                # The recorded 401 EHR-ERR882, then the retry with a fresh token.
                self.requests.append(
                    RecordedRndsRequest(BUNDLE_PATH, headers, bytes(body))
                )
                scripted = self.script.popleft() if self.script else None
            if scripted in _SCRIPTED_FAILURES:
                raise RndsTransportError(_SCRIPTED_FAILURES[scripted])
            if scripted is not None:
                return self._outcome(scripted)
            return self._judge(body, requester_cns)

    def reset(self) -> None:
        """Forget recorded requests, scripted answers and accepted identifiers."""
        with self._lock:
            self.requests.clear()
            self.script.clear()
            self.accepted.clear()


FIXTURE_TRANSPORT: Final = FixtureRndsTransport()


def transport_for(provider: str) -> InteropAdapter:
    """Return the fixture transport for the fixture provider; refuse every other."""
    if provider != FIXTURE_PROVIDER or resolved_data_mode() != SYNTHETIC_DATA_MODE:
        raise RndsUnavailableError
    return FIXTURE_TRANSPORT


def establishment_for(provider: str) -> Establishment:
    """Return the fixture establishment; a real one waits for EG-7 credentialing."""
    if provider != FIXTURE_PROVIDER:
        raise RndsUnavailableError
    return FIXTURE_ESTABLISHMENT
