"""Phase >=1 comms adapter interfaces."""

from typing import Protocol


class CommsAdapter(Protocol):
    """Define the future comms integration boundary."""

    def send_message(self) -> None:
        """Send a patient message through a Phase >=1 integration."""
        ...
