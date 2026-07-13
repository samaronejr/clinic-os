"""Phase >=1 consent adapter interfaces."""

from typing import Protocol


class ConsentAdapter(Protocol):
    """Define the future consent integration boundary."""

    def record_consent(self) -> None:
        """Record patient consent through a Phase >=1 integration."""
        ...
