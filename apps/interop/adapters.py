"""Phase >=1 interop adapter interfaces."""

from typing import Protocol


class InteropAdapter(Protocol):
    """Define the future interop integration boundary."""

    def exchange_clinical_record(self) -> None:
        """Exchange a clinical record through a Phase >=1 integration."""
        ...
