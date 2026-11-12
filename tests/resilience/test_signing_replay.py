"""The synthetic provider's operation identity survives replacement workers."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.prescription.signature_provider import (
    SignatureRequest,
    SyntheticSignatureProvider,
)

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper


def test_replaying_a_request_reuses_the_provider_operation(
    settings: SettingsWrapper,
) -> None:
    settings.PRESCRIPTION_SYNTHETIC_SIGNING = True
    request = SignatureRequest(
        operation_id=uuid4(),
        issuer_id=uuid4(),
        signer_subject="synthetic:replay",
        content=b"synthetic document",
        content_digest=sha256(b"synthetic document").hexdigest(),
    )
    first = SyntheticSignatureProvider().begin(request)
    replacement = SyntheticSignatureProvider().begin(request)
    assert replacement.operation_ref == first.operation_ref
    assert (
        SyntheticSignatureProvider()
        .begin(replace(request, operation_id=uuid4()))
        .operation_ref
        != first.operation_ref
    )
