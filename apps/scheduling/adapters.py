"""Phase >=1 scheduling adapter interfaces."""

from typing import Protocol


class SchedulingAdapter(Protocol):
    """Define the future scheduling integration boundary."""

    def create_appointment(self) -> None:
        """Create an appointment through a Phase >=1 integration."""
        ...
