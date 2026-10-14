"""A software ES256 authenticator producing real WebAuthn wire responses."""

from __future__ import annotations

import hashlib
import json
import secrets
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import Client
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from identity.lifecycle_test_support import verified_client
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph


@dataclass(frozen=True, slots=True)
class AssertionOptions:
    """The authenticator's controlled RP, origin, flags and unsigned counter."""

    rp_id: str = "testserver"
    sign_count: int = 1
    verified: bool = True
    origin: str = "http://testserver"


DEFAULT_ASSERTION = AssertionOptions()


@dataclass(frozen=True, slots=True)
class SoftwareAuthenticator:
    """An ephemeral synthetic private key; verification is never mocked."""

    private_key: ec.EllipticCurvePrivateKey = field(repr=False)
    credential_id: bytes = field(repr=False)

    @classmethod
    def create(cls) -> SoftwareAuthenticator:
        """Create an isolated test authenticator, never a stored credential."""
        return cls(ec.generate_private_key(ec.SECP256R1()), secrets.token_bytes(32))

    def registration(self, *, challenge: str, rp_id: str, verified: bool = True) -> str:
        """Emit a fmt=none attestation containing a genuine COSE public key."""
        public = self.private_key.public_key().public_numbers()
        cose = cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: public.x.to_bytes(32, "big"),
                -3: public.y.to_bytes(32, "big"),
            }
        )
        flags = 0x45 if verified else 0x41
        authenticator_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([flags])
            + struct.pack(">I", 0)
            + bytes(16)
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + cose
        )
        client = json.dumps(
            {
                "type": "webauthn.create",
                "challenge": challenge,
                "origin": "http://testserver",
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode()
        return json.dumps(
            {
                "id": bytes_to_base64url(self.credential_id),
                "rawId": bytes_to_base64url(self.credential_id),
                "type": "public-key",
                "authenticatorAttachment": "platform",
                "response": {
                    "clientDataJSON": bytes_to_base64url(client),
                    "attestationObject": bytes_to_base64url(
                        cbor2.dumps(
                            {
                                "fmt": "none",
                                "authData": authenticator_data,
                                "attStmt": {},
                            }
                        )
                    ),
                    "transports": ["internal"],
                },
            }
        )

    def assertion(
        self,
        *,
        challenge: str,
        user_handle: str,
        options: AssertionOptions = DEFAULT_ASSERTION,
    ) -> str:
        """Sign authenticatorData + SHA256(clientDataJSON), as a real device."""
        # Decode the handle as the actual browser does; no forged verifier result.
        handle = base64url_to_bytes(user_handle)
        client = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": challenge,
                "origin": options.origin,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode()
        data = (
            hashlib.sha256(options.rp_id.encode()).digest()
            + bytes([0x05 if options.verified else 0x01])
            + struct.pack(">I", options.sign_count)
        )
        signature = self.private_key.sign(
            data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
        )
        return json.dumps(
            {
                "id": bytes_to_base64url(self.credential_id),
                "rawId": bytes_to_base64url(self.credential_id),
                "type": "public-key",
                "authenticatorAttachment": "platform",
                "response": {
                    "clientDataJSON": bytes_to_base64url(client),
                    "authenticatorData": bytes_to_base64url(data),
                    "signature": bytes_to_base64url(signature),
                    "userHandle": bytes_to_base64url(handle),
                },
            }
        )


@dataclass(frozen=True, slots=True)
class RegisteredPasskey:
    """One fully registered synthetic authenticator and its private test context."""

    client: Client = field(repr=False)
    authenticator: SoftwareAuthenticator = field(repr=False)
    user_handle: str = field(repr=False)


def registered_passkey(graph: RbacGraph) -> RegisteredPasskey:
    """Register through the real TOTP, WebAuthn and PostgreSQL boundaries."""
    client = verified_client(graph)
    authenticator = SoftwareAuthenticator.create()
    with runtime_role():
        options = client.post(
            "/auth/passkey/register/begin/",
            {"clinic_id": str(graph.clinic_b)},
        ).json()
        response = client.post(
            "/auth/passkey/register/finish/",
            {
                "credential": authenticator.registration(
                    challenge=options["challenge"], rp_id="testserver"
                )
            },
        )
    assert response.status_code == 201
    return RegisteredPasskey(client, authenticator, options["user"]["id"])


def passkey_login(registered: RegisteredPasskey) -> Client:
    """Obtain a signed, rotated, persisted session using the actual assertion API."""
    client = Client()
    with runtime_role():
        options = client.post(
            "/auth/passkey/assert/begin/", {"purpose": "login"}
        ).json()
        response = client.post(
            "/auth/passkey/assert/finish/",
            {
                "credential": registered.authenticator.assertion(
                    challenge=options["challenge"], user_handle=registered.user_handle
                )
            },
        )
    assert response.status_code == 200
    return client
