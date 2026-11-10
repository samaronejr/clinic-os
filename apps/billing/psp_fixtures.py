"""Local recorded PSP adapter: no HTTP, real credentials, payable QR or card data."""

from __future__ import annotations

import hmac
import json
import re
from typing import TYPE_CHECKING, cast

from apps.billing.psp_contracts import (
    FIXTURE_PROVIDER,
    MAX_MINOR,
    PaymentAuthenticationError,
    PaymentEvent,
    PaymentStatus,
    StatementLine,
)
from apps.billing.psp_policy import validate_request

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from apps.billing.psp_contracts import PaymentRequest, PaymentState

OPAQUE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
MAX_BODY = 16384
STATES = frozenset(
    {"pending", "settled", "confirmed", "rejected", "expired", "reversed", "unknown"}
)


class RecordedPaymentProvider:
    """Replay explicitly synthetic financial fixtures, never a named PSP API."""

    provider = FIXTURE_PROVIDER

    def __init__(
        self,
        *,
        secret: bytes,
        records: Mapping[UUID, PaymentStatus],
        statements: tuple[StatementLine, ...] = (),
    ) -> None:
        """Accept only injected rehearsal authentication and recorded responses."""
        self._secret = secret
        self._records = dict(records)
        self._statements = statements

    def lookup(self, *, request: PaymentRequest) -> PaymentStatus | None:
        """Read immutable recorded facts under the original provider key."""
        return self._records.get(request.idempotency_key)

    def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
        """Return an explicitly non-payable dynamic code or invalid-domain link."""
        validate_request(request)
        reference = f"fixture-{request.idempotency_key.hex}"
        code = (
            f"SYNTHETIC-NOT-PAYABLE|{reference}|{request.amount_minor}|BRL"
            if request.method == "pix"
            else ""
        )
        url = (
            f"https://payments.invalid/{request.idempotency_key.hex}"
            if request.method == "card"
            else ""
        )
        return PaymentStatus(
            reference,
            "pending",
            request.amount_minor,
            "BRL",
            1,
            payment_code=code,
            payment_url=url,
        )

    def refund(self, *, request: PaymentRequest) -> PaymentStatus:
        """Return pending only: constructing a refund never confirms its effect."""
        return self.create_charge(request=request)

    def payout(self, *, request: PaymentRequest) -> PaymentStatus:
        """Return pending only: a practitioner payable is not a bank transfer."""
        return self.create_charge(request=request)

    def fetch_statement(self, *, account_reference: UUID) -> tuple[StatementLine, ...]:
        """Replay statement rows; the caller still binds them to its exact book."""
        del account_reference
        return self._statements

    def authenticate(self, *, headers: Mapping[str, str], body: bytes) -> PaymentEvent:
        """Authenticate exact bytes before JSON parsing, then allowlist the facts."""
        signature = headers.get("x-fixture-psp-signature", "")
        if (
            not self._secret
            or type(body) is not bytes
            or len(body) > MAX_BODY
            or not re.fullmatch(r"[0-9a-f]{64}", signature)
            or not hmac.compare_digest(
                signature, hmac.digest(self._secret, body, "sha256").hex()
            )
        ):
            raise PaymentAuthenticationError
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError) as error:
            raise PaymentAuthenticationError from error
        if type(payload) is not dict:
            raise PaymentAuthenticationError
        event_id = payload.get("event_id")
        reference = payload.get("provider_reference")
        status = payload.get("status")
        amount = payload.get("amount_minor")
        currency = payload.get("currency")
        version = payload.get("version")
        if (
            type(event_id) is not str
            or not OPAQUE.fullmatch(event_id)
            or type(reference) is not str
            or not OPAQUE.fullmatch(reference)
            or type(status) is not str
            or status not in STATES
            or type(amount) is not int
            or not 1 <= amount <= MAX_MINOR
            or type(currency) is not str
            or not re.fullmatch(r"[A-Z]{3}", currency)
            or type(version) is not int
            or not 1 <= version <= MAX_MINOR
        ):
            raise PaymentAuthenticationError
        return PaymentEvent(
            event_id, reference, cast("PaymentState", status), amount, currency, version
        )
