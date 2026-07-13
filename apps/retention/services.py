"""Phase >=1 retention service entrypoints."""

from typing import NoReturn


def apply_retention_policy() -> NoReturn:
    """Apply a retention policy when the retention domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
