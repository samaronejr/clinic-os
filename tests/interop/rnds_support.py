"""RNDS fixtures: a consented, acknowledged result world and a local mTLS pair.

The world drives the real services: an ordered and acknowledged synthetic
result, a CPF recorded by the physician, a clinic consent text accepted in a
live patient session, and the clinic's ``rnds`` version moved to the fixture
sandbox through the owner lifecycle. The local pair is a CA, a TLS 1.2 Auth
server that requires client certificates and an EHR server, all built with the
standard library ``ssl`` and ``http.server``; ``cryptography`` only issues the
test certificates and the PKCS#12 bundle.
"""

from __future__ import annotations

import http.server
import json
import ssl
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

from apps.consent.services import prepare_acceptance, publish_text, record_consent
from apps.intake.demographics import add_identifier
from apps.intake.patient_access import (
    issue_invitation,
    patient_session_context,
    redeem_invitation,
)
from apps.orders.services import acknowledge_result, ingest_result
from apps.providers import lifecycle
from apps.tenancy.db import tenant_context
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from orders.support import World, ordered, submission
from patient_service_support import runtime_role
from provider_gate_support import APPROVER, seed_capabilities
from renewal.test_retention import admin

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from uuid import UUID

    from apps.consent.models import ConsentAcceptance
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

CPF = "52998224725"
CONSENT_TEXT = (
    "Texto sintético: autorizo o envio dos meus resultados de exame à RNDS.\n\n"
    "Escolha livre e específica; recusar não impede o atendimento."
)
EXAM: dict[str, object] = {
    "exam_code": "94507-1",
    "sus_subgroup": "0214",
    "result_code": "1",
    "method": "Imunocromatográfico",
    "reference_range": (
        "(1) Detectável = presença de anticorpos; "
        "(2) Não detectável = ausência de anticorpos"
    ),
    "specimen_type": "SGHEM",
    "issued_at": "2026-11-09T10:30:12-03:00",
}


@dataclass(frozen=True, slots=True)
class RndsWorld:
    """One acknowledged result whose patient may or may not have consented."""

    graph: RbacGraph
    version_id: UUID
    enrollment: UUID
    session: UUID
    text_id: UUID
    order: World


def open_homologation(
    graph: RbacGraph, settings: SettingsWrapper, *, clinic: UUID | None = None
) -> UUID:
    """Move one clinic's ``rnds`` version (A by default) to the fixture sandbox."""
    settings.INTEROP_RNDS_HOMOLOGATION = True
    clinic_id = graph.clinic_a if clinic is None else clinic
    with owner_context(graph.organization_a):
        if clinic is None:
            seed_capabilities()
        version = lifecycle.propose_version(
            "rnds",
            clinic_id=clinic_id,
            version_input=lifecycle.VersionInput(
                provider="rnds-fixture", environment="homologation", account="fixture"
            ),
        )
        lifecycle.approve_version("rnds", clinic_id=clinic_id, decision=APPROVER)
        version.refresh_from_db()
        version.state = "sandbox"
        version.save(update_fields=("state", "updated_at"))
    return version.pk


def acknowledged_result(graph: RbacGraph) -> tuple[UUID, World]:
    """Return one current, acknowledged result version and its order."""
    world = ordered(graph)
    return another_result(graph, world), world


def another_result(
    graph: RbacGraph, world: World, *, acknowledged: bool = True
) -> UUID:
    """File one more source result on an existing order, acknowledged or not."""
    with as_actor(graph, graph.physician):
        receipt = ingest_result(clinic_id=graph.clinic_a, submission=submission(world))
        if acknowledged:
            acknowledge_result(
                clinic_id=graph.clinic_a,
                version_id=receipt.version_id,
                disposition="no_action",
            )
    return receipt.version_id


def record_cpf(graph: RbacGraph, enrollment: UUID) -> None:
    with as_actor(graph, graph.physician):
        add_identifier(
            clinic_id=graph.clinic_a, enrollment_id=enrollment, kind="cpf", value=CPF
        )


def patient_session(graph: RbacGraph, enrollment: UUID) -> UUID:
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        invitation = issue_invitation(
            clinic_id=graph.clinic_a, enrollment_id=enrollment
        )
    with runtime_role():
        session = redeem_invitation(graph.clinic_a, invitation.secret)
    assert session is not None
    return session


def publish_consent(graph: RbacGraph) -> UUID:
    manager = admin(graph)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        return publish_text(
            clinic_id=graph.clinic_a, purpose="rnds_exchange", text=CONSENT_TEXT
        ).pk


def accept_consent(session: UUID, text_id: UUID) -> ConsentAcceptance:
    with runtime_role(), patient_session_context(session):
        _, offer = prepare_acceptance(text_id=text_id)
        return record_consent(offer=offer, purpose="rnds_exchange", accepted=True)


def rnds_world(
    graph: RbacGraph, settings: SettingsWrapper, *, consented: bool = True
) -> RndsWorld:
    """Build the full eligible world; ``consented=False`` stops before acceptance."""
    open_homologation(graph, settings)
    version_id, order = acknowledged_result(graph)
    record_cpf(graph, order.enrollment)
    session = patient_session(graph, order.enrollment)
    text_id = publish_consent(graph)
    if consented:
        accept_consent(session, text_id)
    return RndsWorld(graph, version_id, order.enrollment, session, text_id, order)


@dataclass(frozen=True, slots=True)
class Issued:
    """One certificate and its key, PEM-encoded on disk where a server needs it."""

    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def issue(  # noqa: PLR0913 - one value per certificate field under test
    common_name: str,
    *,
    issuer: Issued | None = None,
    ca: bool = False,
    server: bool = False,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> Issued:
    """Issue one synthetic certificate; ``issuer=None`` makes it self-signed."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(
            _name(common_name) if issuer is None else issuer.certificate.subject
        )
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or now - timedelta(days=1))
        .not_valid_after(not_after or now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if server:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
    signer = key if issuer is None else issuer.key
    return Issued(builder.sign(signer, hashes.SHA256()), key)


def write_pem(issued: Issued, directory: Path, name: str) -> tuple[Path, Path]:
    certificate = directory / f"{name}.crt"
    key = directory / f"{name}.key"
    certificate.write_bytes(issued.certificate.public_bytes(serialization.Encoding.PEM))
    key.touch(mode=0o600)
    key.write_bytes(
        issued.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate, key


def pkcs12_bundle(issued: Issued, issuer: Issued, passphrase: bytes) -> bytes:
    """Serialize a client certificate the way an A1 certificate is delivered."""
    return pkcs12.serialize_key_and_certificates(
        b"synthetic-establishment",
        issued.key,
        issued.certificate,
        [issuer.certificate],
        serialization.BestAvailableEncryption(passphrase),
    )


@dataclass
class Recorded:
    """Requests the local servers received and the answers still scripted."""

    tokens: int = 0
    bundles: list[tuple[dict[str, str], bytes]] = field(default_factory=list)
    answers: list[tuple[int, dict[str, str], bytes]] = field(default_factory=list)


class _Handler(http.server.BaseHTTPRequestHandler):
    recorded: ClassVar[Recorded]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args

    def _reply(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/api/token":
            self._reply(404, {}, b"")
            return
        type(self).recorded.tokens += 1
        token = {"access_token": f"local-token-{type(self).recorded.tokens}"}
        self._reply(
            200,
            {"Content-Type": "application/json"},
            json.dumps({**token, "token_type": "jwt", "expires_in": 1800000}).encode(),
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        headers = {key.lower(): value for key, value in self.headers.items()}
        type(self).recorded.bundles.append((headers, body))
        if type(self).recorded.answers:
            self._reply(*type(self).recorded.answers.pop(0))
            return
        location = "https://localhost/api/fhir/r4/Bundle/local-0001"
        self._reply(201, {"Location": location, "Content-Location": location}, b"")


@dataclass(frozen=True, slots=True)
class LocalRnds:
    """The running local Auth and EHR servers and the CA that issued them."""

    ca: Issued
    cafile: Path
    auth_port: int
    ehr_port: int
    recorded: Recorded


def _server_context(cert: Path, key: Path, *, client_ca: Path | None) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # TLS 1.2 verifies the client certificate inside the handshake, so a refusal
    # is observed at connect time rather than at a racing first read.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    if client_ca is not None:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(client_ca)
    return context


@contextmanager
def local_rnds(directory: Path) -> Iterator[LocalRnds]:
    """Run a local Auth (client certificate required) and EHR server pair."""
    ca = issue("Synthetic Test Root CA", ca=True)
    cafile, _ = write_pem(ca, directory, "ca")
    server_cert, server_key = write_pem(
        issue("localhost", issuer=ca, server=True), directory, "server"
    )
    recorded = Recorded()
    handler = type("Handler", (_Handler,), {"recorded": recorded})
    servers = []
    for client_ca in (cafile, None):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.socket = _server_context(
            server_cert, server_key, client_ca=client_ca
        ).wrap_socket(server.socket, server_side=True)
        servers.append(server)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in servers
    ]
    for thread in threads:
        thread.start()
    try:
        yield LocalRnds(
            ca,
            cafile,
            servers[0].server_address[1],
            servers[1].server_address[1],
            recorded,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
