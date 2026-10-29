"""Exercise genuine signatures for every algorithm offered by registration."""

from __future__ import annotations

import secrets
from functools import partial
from typing import TYPE_CHECKING, Final

import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from webauthn import verify_authentication_response, verify_registration_response
from webauthn.helpers import bytes_to_base64url, decode_credential_public_key
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.exceptions import InvalidAuthenticationResponse
from webauthn.registration.generate_registration_options import (
    default_supported_pub_key_algs,
)

from auth.passkey_test_support import (
    AssertionOptions,
    SigningKey,
    SoftwareAuthenticator,
)

if TYPE_CHECKING:
    from collections.abc import Callable

KEY_FACTORIES: Final[dict[COSEAlgorithmIdentifier, Callable[[], SigningKey]]] = {
    COSEAlgorithmIdentifier.ECDSA_SHA_256: lambda: ec.generate_private_key(
        ec.SECP256R1()
    ),
    COSEAlgorithmIdentifier.EDDSA: ed25519.Ed25519PrivateKey.generate,
    COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256: lambda: rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    ),
}


@pytest.mark.parametrize("algorithm", default_supported_pub_key_algs)
@pytest.mark.parametrize("rp_id", ["testserver", "foreign.example"])
def test_returned_algorithm_produces_real_scoped_assertions(
    algorithm: COSEAlgorithmIdentifier, rp_id: str
) -> None:
    # Given a real credential with an algorithm from the unchanged offer list.
    authenticator = SoftwareAuthenticator(
        KEY_FACTORIES[algorithm](), secrets.token_bytes(32), algorithm
    )
    registration_challenge = secrets.token_bytes(32)
    registered = verify_registration_response(
        credential=authenticator.registration(
            challenge=bytes_to_base64url(registration_challenge), rp_id="testserver"
        ),
        expected_challenge=registration_challenge,
        expected_rp_id="testserver",
        expected_origin="http://testserver",
        require_user_verification=True,
    )
    assert (
        decode_credential_public_key(registered.credential_public_key).alg == algorithm
    )
    challenge = secrets.token_bytes(32)
    assertion = authenticator.assertion(
        challenge=bytes_to_base64url(challenge),
        user_handle=bytes_to_base64url(b"synthetic-user"),
        options=AssertionOptions(rp_id=rp_id),
    )
    # When the real verifier checks the assertion against the original RP.
    verify = partial(
        verify_authentication_response,
        credential=assertion,
        expected_challenge=challenge,
        expected_rp_id="testserver",
        expected_origin="http://testserver",
        credential_public_key=registered.credential_public_key,
        credential_current_sign_count=registered.sign_count,
        require_user_verification=True,
    )
    # Then matching RP succeeds and a genuinely signed foreign RP is refused.
    if rp_id == "foreign.example":
        with pytest.raises(InvalidAuthenticationResponse):
            verify()
    else:
        verified = verify()
        assert verified.credential_id == authenticator.credential_id
        assert verified.new_sign_count == 1
