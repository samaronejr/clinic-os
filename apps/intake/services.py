"""Phase >=1 intake service entrypoints."""

from typing import NoReturn


def submit_intake() -> NoReturn:
    """Submit patient intake when the intake domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
