"""Phase >=1 intake adapter interfaces."""

from typing import Protocol


class IntakeAdapter(Protocol):
    """Define the future intake integration boundary."""

    def submit_intake(self) -> None:
        """Submit patient intake through a Phase >=1 integration."""
        ...
