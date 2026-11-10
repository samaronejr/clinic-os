"""Reviewed fiscal inputs and explicit gates; no inferred tax classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final
from uuid import UUID

from apps.providers import services as provider_services

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

MAX_SERVICE_CODE_LENGTH: Final = 32
MAX_TAX_REGIME_LENGTH: Final = 64
MAX_CANCEL_WINDOW_SECONDS: Final = 2**31 - 1


class FiscalRefusal(StrEnum):
    """Closed refusal vocabulary contains no fiscal input or provider text."""

    CONFIGURATION_UNREVIEWED = "fiscal_configuration_unreviewed"
    CONFIGURATION_INVALID = "fiscal_configuration_invalid"
    CAPABILITY_UNAVAILABLE = "nfse_capability_unavailable"
    BLOCKED_ON_EG = "BLOCKED-ON-EG"
    TRANSITION_DENIED = "fiscal_transition_denied"
    CANCEL_DEADLINE = "fiscal_cancel_deadline"
    PROVIDER_RESPONSE_INVALID = "fiscal_provider_response_invalid"
    COMMAND_CONFLICT = "fiscal_command_conflict"


class FiscalRefusedError(ValueError):
    """Return a fixed machine code, never fiscal content or provider messages."""

    def __init__(self, code: FiscalRefusal) -> None:
        """Retain only the closed refusal chosen at the boundary."""
        self.code = code.value
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class FiscalReview:
    """Captured external decisions; constructing this object grants no authority."""

    legal_entity_reference: UUID
    municipality_code: str
    service_code: str
    tax_regime: str
    ibs_cbs_applicability: str
    tax_fields: Mapping[str, object]
    review_reference: UUID | None
    reviewed_at: datetime | None
    expires_at: datetime | None
    review_kind: str
    cancellation_window_seconds: int


def validate_review(review: FiscalReview, *, now: datetime, rehearsal: bool) -> None:
    """Require the correct unexpired review and preserve its declared tax groups."""
    if (
        type(review.review_reference) is not UUID
        or review.reviewed_at is None
        or review.expires_at is None
        or review.reviewed_at.tzinfo is None
        or review.expires_at.tzinfo is None
        or review.reviewed_at > now
        or review.expires_at <= now
        or review.review_kind != ("synthetic_fixture" if rehearsal else "eg9")
    ):
        raise FiscalRefusedError(FiscalRefusal.CONFIGURATION_UNREVIEWED)
    group = review.tax_fields.get("IBSCBS")
    if (
        type(review.legal_entity_reference) is not UUID
        or re.fullmatch(r"[0-9]{7}", review.municipality_code) is None
        or not review.service_code
        or len(review.service_code) > MAX_SERVICE_CODE_LENGTH
        or not review.tax_regime
        or len(review.tax_regime) > MAX_TAX_REGIME_LENGTH
        or not review.tax_fields
        or review.ibs_cbs_applicability not in {"applicable", "not_applicable"}
        or (
            review.ibs_cbs_applicability == "applicable"
            and (
                not isinstance(group, dict)
                or not isinstance(group.get("CST"), str)
                or not group["CST"]
                or not isinstance(group.get("cClassTrib"), str)
                or not group["cClassTrib"]
            )
        )
        or (review.ibs_cbs_applicability == "not_applicable" and group is not None)
        or type(review.cancellation_window_seconds) is not int
        or not 0 < review.cancellation_window_seconds <= MAX_CANCEL_WINDOW_SECONDS
    ):
        raise FiscalRefusedError(FiscalRefusal.CONFIGURATION_INVALID)


def require_fiscal_issuance(
    *, clinic_id: UUID, review: FiscalReview, now: datetime
) -> None:
    """Neither an EG-9 record nor an activated provider can open this source lane."""
    validate_review(review, now=now, rehearsal=False)
    if not provider_services.is_live("nfse", clinic_id=clinic_id):
        raise FiscalRefusedError(FiscalRefusal.CAPABILITY_UNAVAILABLE)
    # The national/aggregator production transports are deliberately not enabled.
    raise FiscalRefusedError(FiscalRefusal.BLOCKED_ON_EG)


def require_transition(
    *,
    status: str,
    next_status: str,
    now: datetime,
    cancel_until: datetime | None,
) -> None:
    """Keep ambiguous submissions submitted and require authorization evidence."""
    if (status, next_status) not in {
        ("draft", "submitted"),
        ("draft", "cancelled"),
        ("submitted", "authorized"),
        ("submitted", "rejected"),
        ("authorized", "cancelled"),
    }:
        raise FiscalRefusedError(FiscalRefusal.TRANSITION_DENIED)
    if (
        status == "authorized"
        and next_status == "cancelled"
        and (cancel_until is None or now >= cancel_until)
    ):
        raise FiscalRefusedError(FiscalRefusal.CANCEL_DEADLINE)
