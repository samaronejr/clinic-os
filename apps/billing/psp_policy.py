"""Deterministic PSP checks; no callback can invent money or tenant authority."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from apps.billing.psp_contracts import (
    MAX_INSTALLMENTS,
    MAX_MINOR,
    PaymentInputError,
)

if TYPE_CHECKING:
    from apps.billing.psp_contracts import (
        PaymentEvent,
        PaymentRequest,
        PaymentStatus,
        StatementLine,
    )


def validate_installments(method: str, installments: int, *, maximum: int) -> None:
    """Apply a bounded exact-clinic card policy; PIX is always one collection."""
    if (
        method not in {"pix", "card"}
        or type(maximum) is not int
        or not 1 <= maximum <= MAX_INSTALLMENTS
        or type(installments) is not int
        or not 1 <= installments <= maximum
        or (method == "pix" and installments != 1)
    ):
        raise PaymentInputError


def validate_request(request: PaymentRequest) -> None:
    """Reject bool, float, overflow and unsupported monetary contracts."""
    if (
        request.kind not in {"charge", "refund", "payout"}
        or type(request.amount_minor) is not int
        or not 1 <= request.amount_minor <= MAX_MINOR
        or request.currency != "BRL"
        or (request.kind != "charge" and request.installments != 1)
    ):
        raise PaymentInputError
    validate_installments(
        request.method, request.installments, maximum=MAX_INSTALLMENTS
    )


def classify_payment(
    request: PaymentRequest,
    authoritative: PaymentStatus | None,
    event: PaymentEvent | None = None,
) -> str:
    """Verify exact independent terms; ambiguous and stale facts never post."""
    validate_request(request)
    if authoritative is None or authoritative.status == "unknown":
        return "unknown"
    if (
        type(authoritative.amount_minor) is not int
        or authoritative.amount_minor != request.amount_minor
        or authoritative.currency != request.currency
        or type(authoritative.version) is not int
        or not 1 <= authoritative.version <= MAX_MINOR
        or type(authoritative.fee_minor) is not int
        or not 0 <= authoritative.fee_minor <= request.amount_minor
        or type(authoritative.provider_reference) is not str
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", authoritative.provider_reference)
    ):
        return "quarantined"
    if event is not None:
        if (
            event.provider_reference != authoritative.provider_reference
            or event.amount_minor != request.amount_minor
            or event.currency != request.currency
        ):
            return "quarantined"
        if event.version < authoritative.version:
            return "superseded"
        if (event.version, event.status) != (
            authoritative.version,
            authoritative.status,
        ):
            return "quarantined"
    terminal = "settled" if request.kind == "charge" else "confirmed"
    return {
        terminal: "verified",
        "pending": "recorded",
        "expired": "recorded",
    }.get(authoritative.status, "quarantined")


def match_statement(
    request: PaymentRequest, status: PaymentStatus | None, line: StatementLine
) -> str:
    """Match provider reference, gross centavos, currency, kind and fee exactly."""
    if status is None:
        return "unknown"
    if classify_payment(request, status) != "verified":
        return "quarantined"
    if (
        line.provider_reference != status.provider_reference
        or type(line.amount_minor) is not int
        or line.amount_minor != status.amount_minor
        or line.currency != status.currency
        or line.kind != request.kind
        or type(line.fee_minor) is not int
        or line.fee_minor != status.fee_minor
    ):
        return "quarantined"
    return "matched"
