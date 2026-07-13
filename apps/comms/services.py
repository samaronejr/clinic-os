"""Phase >=1 comms service entrypoints."""

from typing import NoReturn


def send_message() -> NoReturn:
    """Send a patient message when the comms domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
