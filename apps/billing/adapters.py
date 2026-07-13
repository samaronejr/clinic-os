"""Phase >=1 billing adapter interfaces."""

from typing import Protocol


class BillingAdapter(Protocol):
    """Define the future billing integration boundary."""

    def create_invoice(self) -> None:
        """Create an invoice through a Phase >=1 integration."""
        ...
