"""Exact, identity-free PSP contracts for local recorded sandbox rehearsals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

type PaymentKind = Literal["charge", "refund", "payout"]
type PaymentMethod = Literal["pix", "card"]
type PaymentState = Literal[
    "pending", "settled", "confirmed", "rejected", "expired", "reversed", "unknown"
]
MAX_MINOR = 2**63 - 1
MAX_INSTALLMENTS = 12
FIXTURE_PROVIDER = "recorded-psp-v1"


class PaymentInputError(ValueError):
    """Reject invalid financial or provider facts without echoing input."""

    def __init__(self) -> None:
        """Use one bounded non-identifying refusal."""
        super().__init__("invalid PSP facts")


class PaymentAuthenticationError(Exception):
    """Reject raw provider bytes before any tenant or stored reference lookup."""

    def __init__(self) -> None:
        """Do not expose signatures, bodies, credentials or identifiers."""
        super().__init__("PSP authentication failed")


class PaymentLookupError(Exception):
    """Preserve ambiguity when an authoritative provider query is unavailable."""


class PaymentUnavailableError(Exception):
    """Keep every real provider and paid sandbox leg behind its external gates."""

    def __init__(self) -> None:
        """Expose the unresolved gates, not an inferred approval."""
        super().__init__("BLOCKED-ON-EG-1/EG-5")


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    """Immutable provider input; the key survives worker death and redelivery."""

    idempotency_key: UUID
    kind: PaymentKind
    target_reference: UUID
    method: PaymentMethod
    amount_minor: int
    currency: str = "BRL"
    installments: int = 1
    original_reference: str = ""
    payee_reference: str = ""


@dataclass(frozen=True, slots=True)
class PaymentStatus:
    """Authoritative versioned facts, never a callback's unverified assertion."""

    provider_reference: str
    status: PaymentState
    amount_minor: int
    currency: str
    version: int
    fee_minor: int = 0
    payment_code: str = ""
    payment_url: str = ""


@dataclass(frozen=True, slots=True)
class PaymentEvent:
    """Authenticated facts only; external tenant claims are never carried."""

    event_id: str
    provider_reference: str
    status: PaymentState
    amount_minor: int
    currency: str
    version: int


@dataclass(frozen=True, slots=True)
class StatementLine:
    """One independently retained gross economic fact and separate PSP fee."""

    line_id: str
    provider_reference: str
    amount_minor: int
    currency: str
    fee_minor: int = 0
    kind: PaymentKind = "charge"


class PaymentProvider(Protocol):
    """Require authoritative lookup by the original key for every effect."""

    provider: str

    def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
        """Create a dynamic PIX charge or hosted card link, never a settlement."""
        ...

    def lookup(self, *, request: PaymentRequest) -> PaymentStatus | None:
        """Return authoritative facts by key; None means authoritative not-found."""
        ...

    def refund(self, *, request: PaymentRequest) -> PaymentStatus:
        """Send only an exactly approved refund under its original key."""
        ...

    def payout(self, *, request: PaymentRequest) -> PaymentStatus:
        """Send only an exactly approved payout under its original key."""
        ...

    def fetch_statement(self, *, account_reference: UUID) -> tuple[StatementLine, ...]:
        """Return statement facts; a caller must independently bind every line."""
        ...

    def authenticate(self, *, headers: Mapping[str, str], body: bytes) -> PaymentEvent:
        """Authenticate exact raw bytes before parsing or scope resolution."""
        ...
