"""Phase >=1 ehr adapter interfaces."""

from typing import Protocol


class EhrAdapter(Protocol):
    """Define the future ehr integration boundary."""

    def record_clinical_note(self) -> None:
        """Record a clinical note through a Phase >=1 integration."""
        ...
