"""A software authenticator producing genuinely signed WebAuthn responses."""

from __future__ import annotations

import hashlib
import json
import secrets
import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, assert_never

import cbor2
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from django.test import Client
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.algorithms import (
    get_ec2_sig_alg,
    get_rsa_pkcs1_sig_alg,
    get_rsa_pss_sig_alg,
    is_rsa_pss,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier

from identity.lifecycle_test_support import verified_client
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

type SigningKey = (
    ec.EllipticCurvePrivateKey | ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey
)


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

    private_key: SigningKey = field(repr=False)
    credential_id: bytes = field(repr=False)
    algorithm: COSEAlgorithmIdentifier = COSEAlgorithmIdentifier.ECDSA_SHA_256

    @classmethod
    def create(cls) -> SoftwareAuthenticator:
        """Create an isolated test authenticator, never a stored credential."""
        return cls(ec.generate_private_key(ec.SECP256R1()), secrets.token_bytes(32))

    def registration(self, *, challenge: str, rp_id: str, verified: bool = True) -> str:
        """Emit a fmt=none attestation containing a genuine COSE public key."""
        public_key: dict[int, int | bytes]
        match self.private_key:
            case ec.EllipticCurvePrivateKey() as ec_key:
                ec_public = ec_key.public_key().public_numbers()
                public_key = {
                    1: 2,
                    3: self.algorithm.value,
                    -1: 1,
                    -2: ec_public.x.to_bytes(32, "big"),
                    -3: ec_public.y.to_bytes(32, "big"),
                }
            case ed25519.Ed25519PrivateKey() as ed_key:
                public_key = {
                    1: 1,
                    3: self.algorithm.value,
                    -1: 6,
                    -2: ed_key.public_key().public_bytes_raw(),
                }
            case rsa.RSAPrivateKey() as rsa_key:
                rsa_public = rsa_key.public_key().public_numbers()
                public_key = {
                    1: 3,
                    3: self.algorithm.value,
                    -1: rsa_public.n.to_bytes((rsa_public.n.bit_length() + 7) // 8),
                    -2: rsa_public.e.to_bytes((rsa_public.e.bit_length() + 7) // 8),
                }
            case unreachable:
                assert_never(unreachable)
        cose = cbor2.dumps(public_key)
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
        signed_data = data + hashlib.sha256(client).digest()
        match self.private_key:
            case ec.EllipticCurvePrivateKey() as ec_key:
                signature = ec_key.sign(signed_data, get_ec2_sig_alg(self.algorithm))
            case ed25519.Ed25519PrivateKey() as ed_key:
                assert self.algorithm == COSEAlgorithmIdentifier.EDDSA
                signature = ed_key.sign(signed_data)
            case rsa.RSAPrivateKey() as rsa_key:
                if is_rsa_pss(self.algorithm):
                    digest = get_rsa_pss_sig_alg(self.algorithm)
                    scheme = padding.PSS(
                        mgf=padding.MGF1(digest), salt_length=padding.PSS.MAX_LENGTH
                    )
                    signature = rsa_key.sign(signed_data, scheme, digest)
                else:
                    signature = rsa_key.sign(
                        signed_data,
                        padding.PKCS1v15(),
                        get_rsa_pkcs1_sig_alg(self.algorithm),
                    )
            case unreachable:
                assert_never(unreachable)
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
