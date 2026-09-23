"""Purpose-specific authorization boundary for future consent consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from apps.consent.models import ConsentAcceptance


class ConsentAdapter(Protocol):
    """Check current retained authority at use time, never a cached boolean."""

    def consent_for_future_use(
        self, *, clinic_id: UUID, enrollment_id: UUID, purpose: str
    ) -> ConsentAcceptance | None:
        """Return exact active authority or fail closed; never create consent."""
        ...
