"""Shared clinic billing denial, re-exported at its historical service boundary."""


class BillingAccessDeniedError(Exception):
    """Non-identifying denial for unavailable or unauthorized billing records."""
