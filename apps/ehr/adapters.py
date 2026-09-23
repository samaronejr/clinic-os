"""Typed boundary for explicit internal SOAP draft persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ehr.models import ClinicalDocumentVersion


class EhrAdapter(Protocol):
    """Require exact version and optimistic revision at every persistence caller."""

    def record_clinical_note(
        self,
        *,
        clinic_id: UUID,
        version_id: UUID,
        expected_revision: int,
        content: dict[str, str],
    ) -> ClinicalDocumentVersion:
        """Save through the current actor's clinic-scoped clinical authority."""
        ...
