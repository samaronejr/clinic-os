"""Read-only fiscal summaries; no encrypted taxpayer or artifact content in HTML."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.utils.translation import gettext

from apps.billing.errors import BillingAccessDeniedError
from apps.billing.fiscal_provider import rejection_message
from apps.billing.models import FiscalDocument
from apps.billing.security import financial_actor

if TYPE_CHECKING:
    from uuid import UUID

LABELS: Final = {
    "draft": "Fiscal draft",
    "submitted": "Awaiting fiscal confirmation",
    "authorized": "Fiscal authorization recorded",
    "rejected": "Fiscal document rejected",
    "cancelled": "Fiscal document cancelled",
}


@dataclass(frozen=True, slots=True)
class FiscalSummary:
    """Only opaque identity, closed status and bounded rejection code reach the UI."""

    reference: UUID
    status: str
    rehearsal: bool
    rejection_code: str

    @property
    def label(self) -> str:
        """Translate a stored state through the fixed fiscal vocabulary."""
        return gettext(LABELS[self.status])

    @property
    def document_kind(self) -> str:
        """A fixture authorization is never a legally issued NFS-e."""
        return gettext(
            "NFS-e rehearsal (not a tax invoice)" if self.rehearsal else "NFS-e"
        )

    @property
    def rejection_message(self) -> str:
        """Do not render arbitrary provider descriptions or unreviewed tax advice."""
        return rejection_message(self.rejection_code) if self.rejection_code else ""


def fiscal_summaries(*, clinic_id: UUID) -> tuple[bool, tuple[FiscalSummary, ...]]:
    """Only exact-clinic Finance can read the recent nonclinical fiscal projection."""
    try:
        financial_actor(clinic_id=clinic_id, permission="settlement.post")
    except BillingAccessDeniedError:
        return False, ()
    rows = (
        FiscalDocument.objects.filter(clinic_id=clinic_id)
        .only("id", "status", "rehearsal", "rejection_code")
        .order_by("-created_at", "-id")[:20]
    )
    return True, tuple(
        FiscalSummary(row.pk, row.status, row.rehearsal, row.rejection_code)
        for row in rows
    )
