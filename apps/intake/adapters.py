"""Typed boundary for explicit questionnaire submission."""

from typing import Protocol
from uuid import UUID

from apps.intake.models import QuestionnaireResponse


class IntakeAdapter(Protocol):
    """Submit within the caller's validated patient session context."""

    def submit_intake(
        self, *, response_id: UUID, answers: object, expected_revision: int
    ) -> QuestionnaireResponse:
        """Retain the exact assigned version and validate all required answers."""
        ...
