"""Shared canonical identity-string boundaries."""

import unicodedata

from apps.identity.models import User


def canonicalize_username(value: str) -> str:
    """Return the exact stored and lookup form for a username."""
    return User.normalize_username(value.strip()).casefold()


def canonicalize_email(value: str) -> str:
    """Return the exact stored form for an email address."""
    return unicodedata.normalize("NFC", value.strip()).casefold()
