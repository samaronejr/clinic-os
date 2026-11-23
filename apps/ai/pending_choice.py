"""Patient choice for pending output, not for accepted records or manual care."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.consent.models import ConsentPurpose, ConsentText
from apps.consent.services import consent_for_future_use
from apps.identity.current_context import CurrentActorError

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID


def pending_choice_allows(
    *, clinic_id: UUID, enrollment_id: UUID, generated_at: datetime | None = None
) -> bool:
    """Reject superseded, refused, withdrawn and pre-consent pending drafts.

    Document/administrative features retain their own authorization where no
    AI-assistance text has ever been published. Once published, only a current
    receipt for this exact enrollment works. Chart and audio require opt-in
    unconditionally and do not use this unconfigured-clinic rule.
    """
    try:
        receipt = consent_for_future_use(
            clinic_id=clinic_id,
            enrollment_id=enrollment_id,
            purpose=ConsentPurpose.AI_ASSISTANCE,
        )
    except CurrentActorError:
        # Never interpret records hidden by RLS as "no choice required".
        return False
    if receipt is not None:
        return generated_at is None or receipt.accepted_at <= generated_at
    return not ConsentText.objects.filter(
        clinic_id=clinic_id, purpose=ConsentPurpose.AI_ASSISTANCE
    ).exists()
