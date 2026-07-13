"""Phase >=1 retention adapter interfaces."""

from typing import Protocol


class RetentionAdapter(Protocol):
    """Define the future retention integration boundary."""

    def apply_retention_policy(self) -> None:
        """Apply a retention policy through a Phase >=1 integration."""
        ...
