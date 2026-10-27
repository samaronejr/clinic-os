"""Print one complete synthetic PKI/vector JSON; never use its keys live.

Run once with the locked environment and commit the emitted signature-pki.json.
Fixed dates make tests independent of wall time; keys are intentionally fresh.
No CA private key is emitted. The vector encoder is independent of app code.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import rfc8785
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID, ObjectIdentifier

NOW = datetime(2036, 1, 1, tzinfo=UTC)
SIGNER = "synthetic:fixture:signer-35"
TIMESTAMP_SIGNER = "synthetic:fixture:timestamp-35"
DOCUMENT_PURPOSE = ObjectIdentifier("1.3.6.1.4.1.55555.35.1")
ENCODING = serialization.Encoding.DER
type Issuer = tuple[x509.Certificate, rsa.RSAPrivateKey]


def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def certificate(
    private: rsa.RSAPrivateKey, issuer: Issuer | None, profile: str, serial: int
) -> x509.Certificate:
    ca = profile in {"root", "intermediate"}
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"Synthetic Todo35 {profile}")]
    )
    authority = issuer[1] if issuer else private
    issuer_name = issuer[0].subject if issuer else name
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_name)
        .public_key(private.public_key())
        .serial_number(serial)
        .not_valid_before(datetime(2020 if ca else 2030, 1, 1, tzinfo=UTC))
        .not_valid_after(
            datetime(2035 if profile == "expired" else 2040, 1, 1, tzinfo=UTC)
        )
        .add_extension(
            x509.BasicConstraints(
                ca=ca, path_length=(1 if profile == "root" else 0) if ca else None
            ),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=not ca,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(authority.public_key()),
            critical=False,
        )
    )
    if not ca:
        timestamp = profile == "timestamp"
        builder = builder.add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.TIME_STAMPING if timestamp else DOCUMENT_PURPOSE]
            ),
            critical=True,
        ).add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        TIMESTAMP_SIGNER if timestamp else SIGNER
                    )
                ]
            ),
            critical=False,
        )
    return builder.sign(authority, hashes.SHA256())


def crl(issuer: Issuer, revoked: x509.Certificate | None) -> bytes:
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuer[0].subject)
        .last_update(NOW - timedelta(hours=1))
        .next_update(NOW + timedelta(days=1))
        .add_extension(x509.CRLNumber(1), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer[1].public_key()),
            critical=False,
        )
    )
    if revoked is not None:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(revoked.serial_number)
            .revocation_date(NOW - timedelta(days=1))
            .add_extension(
                x509.CRLReason(x509.ReasonFlags.key_compromise), critical=False
            )
            .build()
        )
    return builder.sign(issuer[1], hashes.SHA256()).public_bytes(ENCODING)


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def signature(private: rsa.RSAPrivateKey, message: bytes) -> bytes:
    return private.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )


def main() -> None:
    root_key, intermediate_key = key(), key()
    root = certificate(root_key, None, "root", 1)
    issuer = (root, root_key)
    intermediate = certificate(intermediate_key, issuer, "intermediate", 2)
    authority = (intermediate, intermediate_key)
    keys = {name: key() for name in ("leaf", "revoked", "expired", "timestamp")}
    certs = {
        name: certificate(private, authority, name, 100 + index)
        for index, (name, private) in enumerate(keys.items())
    }
    certs.update(
        root=root,
        intermediate=intermediate,
        wrong_root=certificate(key(), None, "root", 3),
        broken_intermediate=certificate(key(), issuer, "intermediate", 4),
    )
    content = b"%PDF-1.4\nSynthetic cryptographic fixture; not a legal prescription.\n"
    binding = {
        "operation_ref": "synthetic-recorded-operation",
        "content_digest": sha256(content).hexdigest(),
        "signer_subject": SIGNER,
        "category": "synthetic_non_controlled",
        "approval_digest": "a" * 64,
    }
    artifacts = {}
    for case, leaf in [
        ("valid", "leaf"),
        ("revoked_cert", "revoked"),
        ("expired_cert", "expired"),
        ("invalid_chain", "leaf"),
        ("invalid_timestamp", "leaf"),
    ]:
        signed = signature(
            keys[leaf],
            b"clinic-document-x509-v1\0" + rfc8785.dumps(binding) + b"\0" + content,
        )
        stamp = {"time": NOW.isoformat(), "imprint": sha256(signed).hexdigest()}
        stamp_signature = signature(
            keys["timestamp"], b"clinic-timestamp-x509-v1\0" + rfc8785.dumps(stamp)
        )
        if case == "invalid_timestamp":
            stamp_signature = bytes([stamp_signature[0] ^ 1]) + stamp_signature[1:]
        manifest: dict[str, str | dict[str, str]] = {
            "profile": "clinic-synthetic-x509-v1",
            "binding": binding,
            "signature": encoded(signed),
            "certificate": encoded(certs[leaf].public_bytes(ENCODING)),
            "intermediate": encoded(
                certs[
                    "broken_intermediate" if case == "invalid_chain" else "intermediate"
                ].public_bytes(ENCODING)
            ),
            "timestamp": {
                **stamp,
                "signature": encoded(stamp_signature),
                "certificate": encoded(certs["timestamp"].public_bytes(ENCODING)),
            },
        }
        artifacts[case] = encoded(
            content
            + b"\n%%SYNTHETIC-X509-SIGNATURE\n"
            + rfc8785.dumps(manifest)
            + b"\n%%END-X509-SIGNATURE\n"
        )
    fixture = {
        "schema_version": 1,
        "synthetic": True,
        "now": NOW.isoformat(),
        "signer_subject": SIGNER,
        "certificates": {
            name: encoded(cert.public_bytes(ENCODING)) for name, cert in certs.items()
        },
        "public_test_private_keys": {
            name: private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("ascii")
            for name, private in keys.items()
        },
        "revocation": {
            "root_crl": encoded(crl(issuer, None)),
            "intermediate_crl": encoded(crl(authority, certs["revoked"])),
        },
        "binding": binding,
        "artifacts": artifacts,
    }
    print(json.dumps(fixture, indent=2, sort_keys=True))  # noqa: T201 - fixture CLI


if __name__ == "__main__":
    main()
