"""Payload-free insurance failures."""


class InsuranceDeniedError(Exception):
    """Give unknown, foreign and unauthorized selectors identical denials."""

    def __init__(self) -> None:
        """Expose no selector or clinical content."""
        super().__init__("insurance access denied")


class InsuranceConflictError(Exception):
    """Reject changed replays, stale state and altered sealed content."""

    def __init__(self) -> None:
        """Keep financial and clinical values out of errors."""
        super().__init__("insurance command conflict")


class InsuranceInputError(ValueError):
    """Reject malformed or unsupported insurance input."""

    def __init__(self) -> None:
        """Return a fixed input failure."""
        super().__init__("invalid insurance input")


class InsuranceUnavailableError(Exception):
    """Live payer integrations require external approval."""

    def __init__(self) -> None:
        """Keep the external gate explicit."""
        super().__init__("BLOCKED-ON-EG")
