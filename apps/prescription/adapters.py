"""Phase >=1 prescription adapter interfaces."""

from typing import Protocol


class PrescriptionAdapter(Protocol):
    """Define the future prescription integration boundary."""

    def issue_prescription(self) -> None:
        """Issue a prescription through a Phase >=1 integration."""
        ...
