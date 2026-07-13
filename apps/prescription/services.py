"""Phase >=1 prescription service entrypoints."""

from typing import NoReturn


def issue_prescription() -> NoReturn:
    """Issue a prescription when the prescription domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
