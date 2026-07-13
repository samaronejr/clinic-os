"""Phase >=1 billing service entrypoints."""

from typing import NoReturn


def create_invoice() -> NoReturn:
    """Create an invoice when the billing domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
