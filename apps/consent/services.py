"""Phase >=1 consent service entrypoints."""

from typing import NoReturn


def record_consent() -> NoReturn:
    """Record patient consent when the consent domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
