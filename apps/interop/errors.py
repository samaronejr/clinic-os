"""Closed, non-identifying refusals of the interop domain."""


class InteropAccessDeniedError(Exception):
    """One denial for unknown, foreign and unauthorized selectors alike."""


class InteropConflictError(Exception):
    """A stale revision, reused command or terminal record; nothing is reflected."""


class InteropInputError(ValueError):
    """Input outside the bounded management, partner or export contract."""
