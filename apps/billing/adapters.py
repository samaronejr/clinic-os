"""PIX rehearsal boundary. No provider is selected or authorized by task 6.

The historical Asaas candidate is not approval. There is deliberately no HTTP
client, credential setting, production switch or purported sandbox contract.
Synthetic QR images encode visibly non-payable rehearsal text, not a PIX BR Code.

The payment-event contracts below pin the task-39 reconciliation boundary
shape: authentication runs before any stored charge or tenant is resolved,
and an authoritative provider lookup verifies every event before settlement.
The synthetic adapter authenticates rehearsal events with a configured HMAC
secret and reports only pending/expired authoritative states, so a synthetic
settlement claim can never create a receipt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

import qrcode
from django.conf import settings
from django.utils import timezone

from apps.providers.services import is_live

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from uuid import UUID

SYNTHETIC_PROVIDER = "synthetic-pix-v1"
SYNTHETIC_SIGNATURE_HEADER = "x-synthetic-pix-signature"
MAX_EVENT_ID_LENGTH = 255
MAX_PROVIDER_REFERENCE_LENGTH = 128
CURRENCY_CODE_LENGTH = 3


class PixUnavailableError(Exception):
    """The requested capability lacks approved provider/owner authority."""


class PixResponseError(Exception):
    """Provider facts or QR bytes do not match the exact stored request."""


class PaymentEventAuthenticationError(Exception):
    """Reject a payment event that fails provider authentication."""

    def __init__(self) -> None:
        """Expose one stable non-identifying rejection message."""
        super().__init__("payment event authentication failed")


class PaymentLookupError(Exception):
    """Report that the authoritative provider charge query failed."""

    def __init__(self) -> None:
        """Expose one stable non-identifying failure message."""
        super().__init__("authoritative provider charge query failed")


type PaymentEventStatus = Literal[
    "pending", "settled", "expired", "cancelled", "reversed"
]


@dataclass(frozen=True, slots=True)
class AuthenticatedPaymentEvent:
    """Verified event facts; external tenant claims are never carried."""

    event_id: str
    provider_reference: str
    status: PaymentEventStatus
    amount_minor: int
    currency: str


@dataclass(frozen=True, slots=True)
class PaymentOperationFacts:
    """Stored charge-operation facts handed to the provider lookup."""

    operation_id: UUID
    invoice_reference: UUID
    amount_minor: int
    currency: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderChargeStatus:
    """Authoritative provider-side charge state from the approved query."""

    status: PaymentEventStatus
    amount_minor: int
    currency: str


class PaymentEventAdapter(Protocol):
    """Authenticate raw events and answer authoritative charge lookups."""

    provider: str

    def authenticate(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> AuthenticatedPaymentEvent:
        """Verify the provider signature and return only verified fields."""
        ...

    def lookup(self, operation: PaymentOperationFacts) -> ProviderChargeStatus:
        """Return the authoritative charge state for one stored operation.

        Implementations raise ``PaymentLookupError`` when the provider query
        cannot produce an authoritative answer; reconciliation records the
        authentic event as requiring operator action instead of guessing.
        """
        ...


@dataclass(frozen=True, slots=True)
class PixCapability:
    """Report the registry-gated live answer, never inferred readiness."""

    synthetic_enabled: bool
    real_enabled: bool
    reason: str = "missing_pix_provider_owner_and_sandbox_approval"


def pix_capability() -> PixCapability:
    """Permit only explicit rehearsal opt-in in synthetic data mode."""
    return PixCapability(
        synthetic_enabled=settings.CLINIC_DATA_MODE == "synthetic"
        and getattr(settings, "BILLING_SYNTHETIC_PIX", False) is True,
        real_enabled=is_live("pix", clinic_id=None),
    )


def require_synthetic_pix(*, provider: str = SYNTHETIC_PROVIDER) -> None:
    """Reject real providers even if callers supply credentials or settings."""
    if provider != SYNTHETIC_PROVIDER or not pix_capability().synthetic_enabled:
        raise PixUnavailableError


@dataclass(frozen=True, slots=True)
class PixRequest:
    """Frozen financial request; no patient identity or clinical metadata."""

    operation_id: UUID
    invoice_reference: UUID
    amount_minor: int
    currency: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PixResponse:
    """Exact synthetic facts and image returned by the rehearsal adapter."""

    operation_id: UUID
    invoice_reference: UUID
    amount_minor: int
    currency: str
    expires_at: datetime
    provider_reference: str
    copy_code: str
    qr_base64: str
    synthetic: bool = True


def _synthetic_response(request: PixRequest) -> PixResponse:
    reference = f"synthetic-pix-{request.operation_id}"
    copy_code = (
        f"SYNTHETIC-NOT-PAYABLE|{reference}|{request.invoice_reference}|"
        f"{request.amount_minor}|{request.currency}|{request.expires_at.isoformat()}"
    )
    image = qrcode.make(copy_code)
    output = io.BytesIO()
    image.save(output)
    return PixResponse(
        operation_id=request.operation_id,
        invoice_reference=request.invoice_reference,
        amount_minor=request.amount_minor,
        currency=request.currency,
        expires_at=request.expires_at,
        provider_reference=reference,
        copy_code=copy_code,
        qr_base64=base64.b64encode(output.getvalue()).decode("ascii"),
    )


class SyntheticPixAdapter:
    """Pure local deterministic generator, not an external payment service.

    Replaying a frozen request yields the same reference, expiry and exact bytes.
    No side effect exists between generation and persistence, including on timeout.
    A future real adapter needs approved lookup/idempotency semantics and a durable
    send/reconciliation boundary; it cannot be substituted into this local path.
    """

    provider = SYNTHETIC_PROVIDER

    def create_charge(self, request: PixRequest) -> PixResponse:
        """Generate one explicitly synthetic, non-payable charge response."""
        require_synthetic_pix()
        return _synthetic_response(request)

    def verify(self, request: PixRequest, response: PixResponse) -> None:
        """Bind every fact and the exact image/code to the frozen request."""
        require_synthetic_pix()
        if response != _synthetic_response(request):
            raise PixResponseError

    def authenticate(
        self,
        *,
        headers: Mapping[str, str],
        body: bytes,
    ) -> AuthenticatedPaymentEvent:
        """Verify the rehearsal HMAC signature and return verified fields only.

        The secret lives in the ``BILLING_SYNTHETIC_PIX_SECRET`` setting; an
        absent or malformed secret, signature or payload fails closed. Tenant
        or invoice claims inside the body are never read or returned.
        """
        secret = getattr(settings, "BILLING_SYNTHETIC_PIX_SECRET", None)
        if (
            not pix_capability().synthetic_enabled
            or type(secret) is not str
            or not secret
        ):
            raise PaymentEventAuthenticationError
        signature = headers.get(SYNTHETIC_SIGNATURE_HEADER, "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise PaymentEventAuthenticationError
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise PaymentEventAuthenticationError from error
        if type(payload) is not dict:
            raise PaymentEventAuthenticationError
        event_id = payload.get("event_id")
        provider_reference = payload.get("provider_reference")
        status = payload.get("status")
        amount_minor = payload.get("amount_minor")
        currency = payload.get("currency")
        if (
            type(event_id) is not str
            or not 1 <= len(event_id) <= MAX_EVENT_ID_LENGTH
            or type(provider_reference) is not str
            or not 1 <= len(provider_reference) <= MAX_PROVIDER_REFERENCE_LENGTH
            or status not in ("pending", "settled", "expired", "cancelled", "reversed")
            or type(amount_minor) is not int
            or amount_minor <= 0
            or type(currency) is not str
            or len(currency) != CURRENCY_CODE_LENGTH
        ):
            raise PaymentEventAuthenticationError
        return AuthenticatedPaymentEvent(
            event_id=event_id,
            provider_reference=provider_reference,
            status=status,
            amount_minor=amount_minor,
            currency=currency,
        )

    def lookup(self, operation: PaymentOperationFacts) -> ProviderChargeStatus:
        """Report the deterministic rehearsal state for one stored request.

        A synthetic charge is never payable: the authoritative state is
        pending until its stored expiry, then expired. It can never report
        settled, so a forged or confused settlement claim stays unverified.
        """
        require_synthetic_pix()
        status: PaymentEventStatus = (
            "expired" if operation.expires_at <= timezone.now() else "pending"
        )
        return ProviderChargeStatus(
            status=status,
            amount_minor=operation.amount_minor,
            currency=operation.currency,
        )
