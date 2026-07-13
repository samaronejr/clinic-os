"""Phase >=1 teleconsult adapter interfaces."""

from typing import Protocol


class TeleconsultAdapter(Protocol):
    """Define the future teleconsult integration boundary."""

    def start_teleconsultation(self) -> None:
        """Start a teleconsultation through a Phase >=1 integration."""
        ...
