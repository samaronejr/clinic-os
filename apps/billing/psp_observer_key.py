"""A managed rehearsal credential, separate from callbacks and data encryption."""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

from apps.billing.psp_contracts import PaymentUnavailableError
from apps.core.secrets import SecretUnavailableError, secret_store

if TYPE_CHECKING:
    from uuid import UUID


def observer_key(*, clinic_id: UUID) -> bytes:
    """Derive a clinic-only bearer proof; never send the deployment root to SQL."""
    try:
        root = secret_store().get_secret("psp-observer-v1")
    except SecretUnavailableError as error:
        raise PaymentUnavailableError from error
    return hmac.digest(
        bytes.fromhex(root), b"psp-observer-v1\0" + clinic_id.bytes, "sha256"
    )


def observer_verifier(*, clinic_id: UUID) -> bytes:
    """Only this non-bearer digest is recorded by the authorized policy owner."""
    return hashlib.sha256(observer_key(clinic_id=clinic_id)).digest()
