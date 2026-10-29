"""Date-aware preparation policy; electronic regulated issuance stays closed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.prescription.class_values import PrescriptionClass
from apps.prescription.models import PrescriptionClassPolicy

if TYPE_CHECKING:
    from datetime import date


@dataclass(frozen=True, slots=True, kw_only=True)
class ClassEligibility:
    """An eligibility verdict, never a provider activation or legal approval."""

    allowed: bool
    reason_code: str
    mode: str
    label: str


def class_eligibility(
    *, category: str | None, electronic: bool, on_date: date | None = None
) -> ClassEligibility:
    """Use the recorded date boundary without accepting claimed gate satisfaction."""
    mode = "electronic" if electronic else "paper"
    if category is None:
        return ClassEligibility(
            allowed=False,
            reason_code="class_required",
            mode=mode,
            label=_("Choose a prescription class before issuance."),
        )
    policy = PrescriptionClassPolicy.objects.filter(pk=category).first()
    if policy is None:
        return ClassEligibility(
            allowed=False,
            reason_code="class_electronic_unavailable",
            mode=mode,
            label=_("Prescription class unavailable."),
        )
    if not electronic:
        return ClassEligibility(
            allowed=True,
            reason_code="",
            mode=mode,
            label=_("Paper preparation only; not issued."),
        )
    if category == PrescriptionClass.SYNTHETIC:
        return ClassEligibility(
            allowed=True,
            reason_code="",
            mode=mode,
            label=_("Synthetic rehearsal; no real issuance"),
        )
    today = on_date if on_date is not None else timezone.localdate()
    if policy.requires_sncr and (
        policy.electronic_allowed_from is None
        or today >= policy.electronic_allowed_from
    ):
        return ClassEligibility(
            allowed=False,
            reason_code="sncr_required",
            mode=mode,
            label=_(
                "Electronic issuance unavailable: SNCR integration pending. "
                "Prepare paper instead."
            ),
        )
    # EG-5/EG-6 and the real signing/issuance contracts are not installed here.
    # Dates and a known class never enable a provider or substitute for those gates.
    return ClassEligibility(
        allowed=False,
        reason_code="class_electronic_unavailable",
        mode=mode,
        label=_("Electronic issuance unavailable. Prepare paper instead."),
    )


def require_issuance_class(*, category: str | None) -> None:
    """Refuse before creating or dispatching any signing operation."""
    eligibility = class_eligibility(category=category, electronic=True)
    if not eligibility.allowed:
        raise ValidationError(eligibility.label, code=eligibility.reason_code)
